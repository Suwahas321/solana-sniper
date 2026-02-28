"""
Solana Meme Coin Sniping Bot
Monitors Pump.fun, Raydium, Jupiter for new token launches
Features: Auto-buy, TP/SL, Wallet Management, Telegram Alerts
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import aiohttp
import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.system_program import transfer, TransferParams
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
import websockets

# ── Config ──────────────────────────────────────────────────────────────────
from config import BotConfig, load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("sniper.log"),
    ],
)
log = logging.getLogger("sniper")


# ── Data Models ─────────────────────────────────────────────────────────────
@dataclass
class TokenLaunch:
    mint: str
    name: str
    symbol: str
    platform: str  # pumpfun | raydium | jupiter
    liquidity_sol: float
    timestamp: float = field(default_factory=time.time)
    pool_address: Optional[str] = None


@dataclass
class Position:
    mint: str
    symbol: str
    entry_price: float
    amount_tokens: float
    amount_sol_spent: float
    buy_tx: str
    timestamp: float = field(default_factory=time.time)
    take_profit: float = 0.0   # SOL price target
    stop_loss: float = 0.0     # SOL price target
    status: str = "open"       # open | closed


# ── Telegram ─────────────────────────────────────────────────────────────────
class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{token}"

    async def send(self, msg: str, parse_mode: str = "HTML"):
        if not self.token or not self.chat_id:
            return
        url = f"{self.base}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": msg, "parse_mode": parse_mode}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status != 200:
                        log.warning(f"Telegram error: {await r.text()}")
            except Exception as e:
                log.warning(f"Telegram send failed: {e}")

    async def new_token(self, launch: TokenLaunch):
        msg = (
            f"🚀 <b>NEW TOKEN DETECTED</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"📛 Name: <b>{launch.name}</b> (${launch.symbol})\n"
            f"🏦 Platform: {launch.platform.upper()}\n"
            f"💧 Liquidity: {launch.liquidity_sol:.2f} SOL\n"
            f"🪙 Mint: <code>{launch.mint}</code>\n"
            f"⏱ Time: {datetime.fromtimestamp(launch.timestamp).strftime('%H:%M:%S')}"
        )
        await self.send(msg)

    async def buy_executed(self, pos: Position):
        msg = (
            f"✅ <b>BUY EXECUTED</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🪙 Token: <b>{pos.symbol}</b>\n"
            f"💰 Spent: {pos.amount_sol_spent:.4f} SOL\n"
            f"📊 Entry Price: {pos.entry_price:.10f} SOL\n"
            f"🎯 Take Profit: {pos.take_profit:.10f} SOL\n"
            f"🛑 Stop Loss: {pos.stop_loss:.10f} SOL\n"
            f"🔗 TX: <code>{pos.buy_tx}</code>"
        )
        await self.send(msg)

    async def sell_executed(self, pos: Position, exit_price: float, pnl_sol: float, reason: str):
        emoji = "🟢" if pnl_sol > 0 else "🔴"
        msg = (
            f"{emoji} <b>SELL EXECUTED ({reason})</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🪙 Token: <b>{pos.symbol}</b>\n"
            f"📈 Entry: {pos.entry_price:.10f} SOL\n"
            f"📉 Exit:  {exit_price:.10f} SOL\n"
            f"💵 PnL: <b>{pnl_sol:+.4f} SOL</b>"
        )
        await self.send(msg)

    async def error(self, msg: str):
        await self.send(f"⚠️ <b>ERROR</b>\n{msg}")


# ── Wallet Manager ────────────────────────────────────────────────────────────
class WalletManager:
    def __init__(self, private_key: str, rpc_url: str):
        key_bytes = base58.b58decode(private_key)
        self.keypair = Keypair.from_bytes(key_bytes)
        self.pubkey = self.keypair.pubkey()
        self.client = AsyncClient(rpc_url, commitment=Confirmed)

    async def get_balance_sol(self) -> float:
        resp = await self.client.get_balance(self.pubkey)
        return resp.value / 1e9

    async def close(self):
        await self.client.close()

    def sign_transaction(self, tx: Transaction) -> Transaction:
        tx.sign([self.keypair])
        return tx


# ── Price Feed ────────────────────────────────────────────────────────────────
class PriceFeed:
    JUPITER_PRICE_URL = "https://price.jup.ag/v6/price"
    SOL_MINT = "So11111111111111111111111111111111111111112"

    async def get_price_sol(self, mint: str) -> Optional[float]:
        """Get token price in SOL via Jupiter Price API"""
        url = f"{self.JUPITER_PRICE_URL}?ids={mint}&vsToken={self.SOL_MINT}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json()
                    price = data.get("data", {}).get(mint, {}).get("price")
                    return float(price) if price else None
            except Exception as e:
                log.warning(f"Price fetch failed for {mint}: {e}")
                return None


# ── Jupiter Swap ─────────────────────────────────────────────────────────────
class JupiterSwapper:
    QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
    SWAP_URL = "https://quote-api.jup.ag/v6/swap"
    SOL_MINT = "So11111111111111111111111111111111111111112"

    def __init__(self, wallet: WalletManager, slippage_bps: int = 300):
        self.wallet = wallet
        self.slippage_bps = slippage_bps  # 3% default slippage

    async def get_quote(self, input_mint: str, output_mint: str, amount_lamports: int) -> Optional[dict]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_lamports),
            "slippageBps": self.slippage_bps,
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(self.QUOTE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        return await r.json()
            except Exception as e:
                log.error(f"Quote error: {e}")
        return None

    async def swap(self, input_mint: str, output_mint: str, amount_lamports: int) -> Optional[str]:
        """Execute swap, return tx signature or None"""
        quote = await self.get_quote(input_mint, output_mint, amount_lamports)
        if not quote:
            log.error("Failed to get quote")
            return None

        swap_payload = {
            "quoteResponse": quote,
            "userPublicKey": str(self.wallet.pubkey),
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": 100_000,  # priority fee for speed
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(self.SWAP_URL, json=swap_payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        log.error(f"Swap API error: {await r.text()}")
                        return None
                    swap_data = await r.json()
            except Exception as e:
                log.error(f"Swap request error: {e}")
                return None

        # Deserialize, sign, and send transaction
        try:
            import base64
            from solders.transaction import VersionedTransaction
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = self.wallet.keypair.sign_message(bytes(tx.message))

            # Send via RPC
            resp = await self.wallet.client.send_raw_transaction(
                bytes(tx),
                opts={"skip_preflight": False, "preflight_commitment": "confirmed"},
            )
            sig = str(resp.value)
            log.info(f"Swap TX: {sig}")
            return sig
        except Exception as e:
            log.error(f"Transaction signing/sending error: {e}")
            return None

    async def buy_token(self, mint: str, sol_amount: float) -> Optional[str]:
        lamports = int(sol_amount * 1e9)
        return await self.swap(self.SOL_MINT, mint, lamports)

    async def sell_token(self, mint: str, token_amount: float, decimals: int = 6) -> Optional[str]:
        raw_amount = int(token_amount * (10 ** decimals))
        return await self.swap(mint, self.SOL_MINT, raw_amount)


# ── Pump.fun Monitor ──────────────────────────────────────────────────────────
class PumpFunMonitor:
    WS_URL = "wss://pumpportal.fun/api/data"

    def __init__(self, callback):
        self.callback = callback  # async fn(TokenLaunch)
        self.running = False

    async def start(self):
        self.running = True
        while self.running:
            try:
                async with websockets.connect(self.WS_URL, ping_interval=20) as ws:
                    # Subscribe to new token events
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    log.info("✅ Connected to Pump.fun WebSocket")

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            if data.get("txType") == "create":
                                launch = TokenLaunch(
                                    mint=data.get("mint", ""),
                                    name=data.get("name", "Unknown"),
                                    symbol=data.get("symbol", "???"),
                                    platform="pumpfun",
                                    liquidity_sol=float(data.get("solAmount", 0)),
                                    pool_address=data.get("bondingCurveKey"),
                                )
                                await self.callback(launch)
                        except Exception as e:
                            log.warning(f"PumpFun parse error: {e}")
            except Exception as e:
                log.error(f"PumpFun WS error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    def stop(self):
        self.running = False


# ── Raydium Monitor ───────────────────────────────────────────────────────────
class RadyiumMonitor:
    """Monitors Raydium for new AMM pool creations via RPC log subscription"""
    RAYDIUM_AMM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

    def __init__(self, rpc_ws_url: str, callback):
        self.rpc_ws_url = rpc_ws_url
        self.callback = callback
        self.running = False

    async def start(self):
        self.running = True
        while self.running:
            try:
                async with websockets.connect(self.rpc_ws_url) as ws:
                    sub_msg = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [self.RAYDIUM_AMM]},
                            {"commitment": "confirmed"},
                        ],
                    }
                    await ws.send(json.dumps(sub_msg))
                    log.info("✅ Connected to Raydium log monitor")

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            result = data.get("params", {}).get("result", {})
                            logs = result.get("value", {}).get("logs", [])

                            # Look for pool initialization
                            if any("initialize2" in l or "InitializeInstruction2" in l for l in logs):
                                sig = result.get("value", {}).get("signature", "")
                                # You'd parse the transaction for mint details
                                # Simplified placeholder:
                                launch = TokenLaunch(
                                    mint="UNKNOWN",  # Parse from tx
                                    name="Raydium Token",
                                    symbol="???",
                                    platform="raydium",
                                    liquidity_sol=0.0,
                                    pool_address=sig,
                                )
                                await self.callback(launch)
                        except Exception as e:
                            log.warning(f"Raydium parse error: {e}")
            except Exception as e:
                log.error(f"Raydium WS error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    def stop(self):
        self.running = False


# ── Position Manager (TP/SL) ──────────────────────────────────────────────────
class PositionManager:
    def __init__(self, swapper: JupiterSwapper, price_feed: PriceFeed, notifier: TelegramNotifier, cfg: BotConfig):
        self.swapper = swapper
        self.price_feed = price_feed
        self.notifier = notifier
        self.cfg = cfg
        self.positions: dict[str, Position] = {}
        self.running = False

    def add(self, pos: Position):
        self.positions[pos.mint] = pos
        log.info(f"Position opened: {pos.symbol} | Entry: {pos.entry_price:.10f} SOL")

    async def monitor_loop(self):
        self.running = True
        while self.running:
            for mint, pos in list(self.positions.items()):
                if pos.status != "open":
                    continue
                price = await self.price_feed.get_price_sol(mint)
                if price is None:
                    continue

                reason = None
                if price >= pos.take_profit:
                    reason = "TAKE PROFIT"
                elif price <= pos.stop_loss:
                    reason = "STOP LOSS"

                if reason:
                    log.info(f"{reason} triggered for {pos.symbol} at {price:.10f}")
                    tx = await self.swapper.sell_token(mint, pos.amount_tokens)
                    if tx:
                        pos.status = "closed"
                        pnl = (price - pos.entry_price) * pos.amount_tokens
                        await self.notifier.sell_executed(pos, price, pnl, reason)
                        del self.positions[mint]

            await asyncio.sleep(self.cfg.price_check_interval)

    def stop(self):
        self.running = False


# ── Main Bot ──────────────────────────────────────────────────────────────────
class SniperBot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.wallet = WalletManager(cfg.private_key, cfg.rpc_url)
        self.notifier = TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)
        self.swapper = JupiterSwapper(self.wallet, slippage_bps=cfg.slippage_bps)
        self.price_feed = PriceFeed()
        self.position_mgr = PositionManager(self.swapper, self.price_feed, self.notifier, cfg)
        self.seen_tokens: set[str] = set()
        self.running = False

    async def on_token_launch(self, launch: TokenLaunch):
        """Called when a new token is detected"""
        if launch.mint in self.seen_tokens or launch.mint == "UNKNOWN":
            return
        self.seen_tokens.add(launch.mint)

        log.info(f"🚀 New token: {launch.name} ({launch.symbol}) on {launch.platform} | Liq: {launch.liquidity_sol:.2f} SOL")

        # Filters
        if launch.liquidity_sol < self.cfg.min_liquidity_sol:
            log.info(f"  Skipped: liquidity {launch.liquidity_sol:.2f} < min {self.cfg.min_liquidity_sol}")
            return

        await self.notifier.new_token(launch)

        # Check wallet balance
        balance = await self.wallet.get_balance_sol()
        if balance < self.cfg.buy_amount_sol + 0.01:  # keep 0.01 for fees
            log.warning(f"Insufficient balance: {balance:.4f} SOL")
            await self.notifier.error(f"Insufficient balance: {balance:.4f} SOL")
            return

        # Execute buy
        log.info(f"  Buying {self.cfg.buy_amount_sol} SOL of {launch.symbol}...")
        tx = await self.swapper.buy_token(launch.mint, self.cfg.buy_amount_sol)

        if not tx:
            log.error(f"  Buy failed for {launch.symbol}")
            await self.notifier.error(f"Buy FAILED for {launch.symbol} ({launch.mint})")
            return

        # Get entry price
        await asyncio.sleep(3)  # wait for tx to confirm
        entry_price = await self.price_feed.get_price_sol(launch.mint)
        if not entry_price:
            entry_price = 0.0

        # Create position with TP/SL
        tokens_received = (self.cfg.buy_amount_sol / entry_price) if entry_price > 0 else 0
        pos = Position(
            mint=launch.mint,
            symbol=launch.symbol,
            entry_price=entry_price,
            amount_tokens=tokens_received,
            amount_sol_spent=self.cfg.buy_amount_sol,
            buy_tx=tx,
            take_profit=entry_price * self.cfg.take_profit_multiplier,
            stop_loss=entry_price * self.cfg.stop_loss_multiplier,
        )

        self.position_mgr.add(pos)
        await self.notifier.buy_executed(pos)

    async def run(self):
        self.running = True
        balance = await self.wallet.get_balance_sol()
        log.info(f"🤖 Sniper Bot Started | Wallet: {self.wallet.pubkey} | Balance: {balance:.4f} SOL")

        await self.notifier.send(
            f"🤖 <b>Sniper Bot Online</b>\n"
            f"Wallet: <code>{self.wallet.pubkey}</code>\n"
            f"Balance: {balance:.4f} SOL\n"
            f"Buy Amount: {self.cfg.buy_amount_sol} SOL\n"
            f"TP: {self.cfg.take_profit_multiplier}x | SL: {self.cfg.stop_loss_multiplier}x"
        )

        # Start all tasks concurrently
        tasks = [
            asyncio.create_task(self.position_mgr.monitor_loop()),
            asyncio.create_task(PumpFunMonitor(self.on_token_launch).start()),
        ]

        if self.cfg.monitor_raydium:
            rpc_ws = self.cfg.rpc_url.replace("https://", "wss://").replace("http://", "ws://")
            tasks.append(asyncio.create_task(RadyiumMonitor(rpc_ws, self.on_token_launch).start()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("Bot stopped.")
        finally:
            await self.wallet.close()


# ── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cfg = load_config()
    bot = SniperBot(cfg)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("Interrupted by user. Goodbye!")
