"""
Bot Configuration
Edit config.json or set environment variables to configure the bot.
"""

import json
import os
from dataclasses import dataclass


@dataclass
class BotConfig:
    # Wallet
    private_key: str             # Base58 private key

    # RPC
    rpc_url: str                 # e.g. https://api.mainnet-beta.solana.com or Helius/QuickNode

    # Trading
    buy_amount_sol: float        # SOL to spend per snipe
    min_liquidity_sol: float     # Minimum pool liquidity to snipe
    slippage_bps: int            # Slippage in basis points (300 = 3%)

    # Take Profit / Stop Loss (multipliers from entry)
    take_profit_multiplier: float   # e.g. 2.0 = 2x = +100%
    stop_loss_multiplier: float     # e.g. 0.5 = 50% loss

    # Telegram
    telegram_token: str
    telegram_chat_id: str

    # Monitoring
    monitor_raydium: bool = True
    monitor_pumpfun: bool = True
    price_check_interval: float = 5.0   # seconds between TP/SL checks


def load_config(path: str = "config.json") -> BotConfig:
    """Load config from JSON file, with env var overrides"""
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
    else:
        data = {}

    # Environment variable overrides (useful for Docker/CI)
    def get(key: str, default=None):
        return os.environ.get(key.upper(), data.get(key, default))

    cfg = BotConfig(
        private_key=get("private_key", ""),
        rpc_url=get("rpc_url", "https://api.mainnet-beta.solana.com"),
        buy_amount_sol=float(get("buy_amount_sol", 0.1)),
        min_liquidity_sol=float(get("min_liquidity_sol", 5.0)),
        slippage_bps=int(get("slippage_bps", 300)),
        take_profit_multiplier=float(get("take_profit_multiplier", 2.0)),
        stop_loss_multiplier=float(get("stop_loss_multiplier", 0.5)),
        telegram_token=get("telegram_token", ""),
        telegram_chat_id=get("telegram_chat_id", ""),
        monitor_raydium=bool(get("monitor_raydium", True)),
        monitor_pumpfun=bool(get("monitor_pumpfun", True)),
        price_check_interval=float(get("price_check_interval", 5.0)),
    )

    if not cfg.private_key:
        raise ValueError("❌ private_key is required in config.json or env var PRIVATE_KEY")
    if not cfg.rpc_url:
        raise ValueError("❌ rpc_url is required")

    return cfg
