# PredictX — AI-Powered Quantitative Trading Platform

PredictX is a production-structured, asynchronous quantitative trading platform designed for automated cryptocurrency market analysis, strategy execution, risk management, portfolio tracking, and real-time monitoring using the **Binance API**.

The platform provides a modular architecture for connecting market data, trading strategies, execution, risk controls, portfolio management, monitoring, and analytics into a single automated trading system.

> ⚠️ **Safety First**
>
> PredictX runs in **paper/dry-run mode by default**. Orders are simulated and are not submitted to Binance. Real trading should only be enabled explicitly after validating the strategy, configuration, and risk controls.
>
> **Live trading involves real financial risk. Never enable it without thoroughly testing the system.**

> 🔐 **Secrets**
>
> Never commit API keys, API secrets, private credentials, or `.env` files to GitHub. Use environment variables or a dedicated secrets manager. If credentials have ever been exposed, revoke and rotate them immediately.

---

## 🚀 Core Features

* 🤖 Automated cryptocurrency trading
* 📊 Real-time Binance market data
* ⚡ Fully asynchronous trading engine
* 🧠 Pluggable quantitative strategies
* 📈 Technical-indicator based analysis
* 🛡️ Multi-layer risk management
* 💰 Portfolio and P&L tracking
* 🔄 Automated order management
* 📡 WebSocket market-data streaming
* 🌐 REST API integration
* 📋 Structured trading logs
* ❤️ Real-time system heartbeat and health monitoring
* 🧪 Offline testing without network access
* 🖥️ Trading dashboard and monitoring
* 🛑 Emergency stop / kill switch
* 🐳 Docker-ready architecture
* ⚙️ Production-oriented configuration

---

# 🏗️ Architecture

```text
                         ┌──────────────────────┐
                         │      PredictX        │
                         │ Trading Platform     │
                         └──────────┬───────────┘
                                    │
             ┌──────────────────────┼──────────────────────┐
             │                      │                      │
             ▼                      ▼                      ▼
       Configuration          Market Data             Strategies
             │                      │                      │
             │              ┌───────┴────────┐             │
             │              │                │             │
             │           Binance WS       Binance REST     │
             │              │                │             │
             │              └───────┬────────┘             │
             │                      │                      │
             └──────────────────────┼──────────────────────┘
                                    ▼
                           ┌─────────────────┐
                           │ Strategy Engine │
                           └────────┬────────┘
                                    │
                                    ▼
                           ┌─────────────────┐
                           │  Risk Engine    │
                           └────────┬────────┘
                                    │
                                    ▼
                           ┌─────────────────┐
                           │ Order Manager   │
                           └────────┬────────┘
                                    │
                                    ▼
                           ┌─────────────────┐
                           │ Binance Broker  │
                           └────────┬────────┘
                                    │
                                    ▼
                              Binance API
                                    │
             ┌──────────────────────┼──────────────────────┐
             ▼                      ▼                      ▼
        Positions                Orders                  Fills
             │                      │                      │
             └──────────────────────┼──────────────────────┘
                                    ▼
                           Portfolio Manager
                                    │
                                    ▼
                         Monitoring / Dashboard
```

---

# 📦 Project Structure

```text
PredictX/
│
├── run.py
├── run_paper.py
├── run_dashboard.py
├── requirements.txt
├── env.example.txt
│
├── config/
│   ├── settings.py
│   └── exchange/
│
├── exchange/
│   ├── binance/
│   │   ├── client.py
│   │   ├── websocket.py
│   │   └── authentication.py
│   └── market_registry.py
│
├── execution/
│   ├── executor.py
│   ├── order_manager.py
│   └── strategy_runner.py
│
├── strategies/
│   ├── base.py
│   └── ...
│
├── risk/
│   └── engine.py
│
├── portfolio/
│   └── manager.py
│
├── market_data/
│   └── models.py
│
├── monitoring/
│   ├── health.py
│   └── metrics.py
│
├── api/
│   └── rest_client.py
│
├── dashboard/
│
├── tests/
│   ├── test_strategy.py
│   ├── test_engine.py
│   └── test_startup.py
│
├── data/
│   ├── live/
│   ├── paper/
│   └── dashboard/
│
├── logs/
├── state/
│
└── deploy/
    └── install.sh
```

---

# 🔄 Trading Flow

```text
Binance WebSocket
        │
        ▼
Real-Time Market Data
        │
        ▼
MarketState
        │
        ▼
Strategy Engine
        │
        ▼
Trading Signal
        │
        ▼
Risk Engine
        │
        ├── ❌ Rejected
        │
        └── ✅ Approved
                 │
                 ▼
           Order Manager
                 │
                 ▼
          Binance API
                 │
                 ▼
          Order / Fill
                 │
                 ▼
        Portfolio Manager
                 │
                 ▼
          P&L / Metrics
```

---

# 🧠 Strategy Engine

PredictX uses a modular strategy architecture so trading strategies can be added, tested, and replaced without modifying the core execution engine.

A strategy can consume:

* Price data
* Candlestick/OHLCV data
* Trading volume
* Market volatility
* Technical indicators
* Order-book information
* Position state
* Portfolio exposure
* Historical market data

Example strategy pipeline:

```text
Market Data
     ↓
Feature Engineering
     ↓
Technical Indicators
     ↓
Signal Generation
     ↓
Confidence / Validation
     ↓
Risk Check
     ↓
Order Execution
```

Strategies are isolated from the execution layer, allowing the same strategy to operate in both **paper trading** and **live trading**.

---

# 🛡️ Risk Management

Every order passes through the centralized risk engine before execution.

PredictX can enforce:

* Maximum order value
* Maximum position size
* Maximum portfolio exposure
* Maximum number of open positions
* Maximum number of open orders
* Daily loss limit
* Drawdown protection
* Per-symbol cooldown
* Duplicate-order prevention
* Available-balance validation
* Position-size validation
* Emergency trading halt
* Global kill switch

```text
Trading Signal
      │
      ▼
┌─────────────────────┐
│     Risk Engine     │
├─────────────────────┤
│ Balance Check       │
│ Position Limit      │
│ Exposure Limit      │
│ Daily Loss Limit    │
│ Drawdown Protection │
│ Duplicate Check     │
│ Kill Switch         │
└──────────┬──────────┘
           │
      ┌────┴────┐
      │         │
    BLOCK     APPROVE
      │         │
      ▼         ▼
     STOP     EXECUTE
```

---

# 💹 Binance Integration

PredictX integrates with Binance through REST and WebSocket interfaces.

### REST API

Used for:

* Account information
* Balance retrieval
* Order submission
* Order cancellation
* Order status
* Position information
* Historical market data
* Reconciliation

### WebSocket

Used for:

* Real-time price updates
* Candlestick streams
* Trade streams
* Order updates
* Account updates
* Execution updates

The system combines WebSocket streaming with REST-based reconciliation to maintain consistency during connection failures or missed events.

---

# 🧪 Paper Trading

PredictX supports a completely isolated paper-trading environment.

```bash
python run_paper.py
```

Paper trading:

* Uses simulated orders
* Does not submit real orders
* Uses separate state
* Uses separate logs
* Uses separate database/data
* Uses the same strategy framework
* Uses the same risk engine
* Uses the same portfolio accounting

This allows strategies to be validated before introducing real capital.

---

# 🔴 Live Trading

Live trading must be explicitly enabled.

```bash
ENABLE_LIVE_TRADING=true python run.py
```

**Do not enable live trading until you have:**

1. Tested the strategy extensively.
2. Verified Binance API permissions.
3. Confirmed position sizing.
4. Tested the risk engine.
5. Reviewed paper-trading results.
6. Verified API credentials.
7. Tested emergency shutdown procedures.

---

# ⚙️ Configuration

Create your environment configuration from the example:

```bash
cp env.example.txt .env
```

Example:

```env
BINANCE_API_KEY=your_api_key
BINANCE_API_SECRET=your_api_secret

ENABLE_LIVE_TRADING=false

SYMBOLS=BTCUSDT,ETHUSDT

MAX_ORDER_VALUE=10
MAX_POSITION_SIZE=100
MAX_TOTAL_EXPOSURE=100

MAX_DAILY_LOSS=10
MAX_OPEN_POSITIONS=3

HEARTBEAT_INTERVAL_S=30
```

**Never commit `.env` to GitHub.**

Add it to `.gitignore`:

```gitignore
.env
env.txt
*.secret
*.key
```

---

# 🚀 Installation

Clone the repository:

```bash
git clone <YOUR_REPOSITORY_URL>
cd PredictX
```

Create a Python virtual environment:

```bash
python3 -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Configure environment variables:

```bash
cp env.example.txt .env
```

Then start with paper trading.

---

# ▶️ Running PredictX

### Paper Trading

```bash
python run_paper.py
```

### Live Trading

```bash
ENABLE_LIVE_TRADING=true python run.py
```

### Dashboard

```bash
python run_dashboard.py
```

---

# 🛑 Emergency Kill Switch

PredictX provides a filesystem-based emergency kill switch.

```bash
touch state/KILL_SWITCH
```

When triggered, the system:

```text
KILL SWITCH
     │
     ├── Stop new orders
     ├── Cancel eligible open orders
     ├── Stop strategy execution
     └── Preserve current state
```

Remove the file to allow trading to resume:

```bash
rm state/KILL_SWITCH
```

---

# 📊 Logging & Monitoring

PredictX maintains structured logs for different system components.

```text
logs/
├── trade.log
├── orders.log
├── strategy.log
├── execution.log
├── websocket.log
└── errors.log
```

The system can monitor:

* Account balance
* Open orders
* Positions
* Realized P&L
* Unrealized P&L
* Strategy status
* API latency
* WebSocket connectivity
* Order execution
* Risk events
* System health

A state snapshot can be stored during shutdown:

```text
state/last_state.json
```

---

# 🧪 Testing

PredictX is designed to support offline testing without requiring live Binance connectivity.

```bash
python -m tests.test_strategy
python -m tests.test_engine
python -m tests.test_startup
```

Testing covers areas such as:

* Strategy signal generation
* Order creation
* Risk validation
* Order management
* Portfolio accounting
* Paper execution
* Startup validation
* Shutdown and recovery

---

# 🔐 Security

Security is a core part of PredictX.

### Never commit:

```text
.env
env.txt
API keys
API secrets
private keys
wallet credentials
database credentials
production logs
```

Use environment variables or a secrets manager.

For Binance API keys, enable only the permissions required by the application and avoid unnecessary withdrawal permissions.

---

# 🏭 Production
