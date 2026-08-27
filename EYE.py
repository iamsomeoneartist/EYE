import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime
from flask import Flask, send_from_directory
import numpy as np
import pandas as pd
import requests

# ==========================================
# 1. Flask 서버 설정 및 로깅 통제
# ==========================================
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

web_app = Flask(__name__, static_folder='logs')

@web_app.route('/')
def health_check():
    return f"EYE Professional System Active! Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 200

@web_app.route('/logs/<path:filename>')
def serve_logs(filename):
    """GitHub Actions 호스팅 시 로컬 로그 정적 서버 제공 지원"""
    return send_from_directory('logs', filename)

def run_flask():
    web_app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)

# ==========================================
# 2. 리스크 및 파라미터 (Notebook 원칙 준수)
# ==========================================
INITIAL_SEED = 100000.0        # 초기 시드: 10만원
MAX_RISK_PER_TRADE = 0.015     # 1회 최대 허용 손실: 1.5% (1,500원)
MIN_ORDER_VALUE = 5000.0       # 최소 주문 금액
SLIPPAGE_RATE = 0.001          # 가상 슬리피지 (0.1%)
TARGET_ALTS = ["XRP_KRW", "ITH_KRW", "SOL_KRW", "DOGE_KRW"]

class EYETRADER:
    def __init__(self):
        self.balance = INITIAL_SEED
        self.active_position = None
        self.btc_status = "STABLE"
        self.btc_change = 0.0
        self.binance_oi = 0.0
        self.journal = []
        self.scan_results = []
        os.makedirs("logs", exist_ok=True)

    # --------------------------------------
    # [1. 시장 데이터 수집 모듈]
    # --------------------------------------
    def fetch_ohlcv(self, symbol, interval="15m", count=50):
        try:
            url = f"https://api.bithumb.com/public/candlestick/{symbol}/{interval}"
            res = requests.get(url, timeout=3).json()
            if res.get("status") == "0000":
                df = pd.DataFrame(res["data"][-count:], columns=["time", "open", "high", "low", "close", "volume"])
                df["time"] = pd.to_datetime(df["time"].astype(int), unit="ms")
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = df[col].astype(float)
                return df
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERR] 데이터 수집 실패 ({symbol}): {e}")
        return pd.DataFrame()

    def fetch_binance_oi(self):
        """글로벌 파생상품 미결제약정 수집"""
        try:
            url = "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
            res = requests.get(url, timeout=3).json()
            self.binance_oi = float(res.get("openInterest", 0))
        except:
            self.binance_oi = 0.0

    # --------------------------------------
    # [2 & 3. 시장 상태 분석 및 전략 엔진]
    # --------------------------------------
    def analyze_strategy_signals(self, df):
        if df.empty or len(df) < 20:
            return df
            
        df['sma'] = df['close'].rolling(window=20).mean()
        df['std'] = df['close'].rolling(window=20).std()
        df['bb_upper'] = df['sma'] + (2.0 * df['std'])
        df['bb_lower'] = df['sma'] - (2.0 * df['std'])

        df['tr'] = np.maximum(df['high'] - df['low'], 
                              np.maximum(abs(df['high'] - df['close'].shift(1)), 
                                         abs(df['low'] - df['close'].shift(1))))
        df['atr'] = df['tr'].rolling(window=20).mean()
        df['kc_upper'] = df['sma'] + (df['atr'] * 1.5)
        df['kc_lower'] = df['sma'] - (df['atr'] * 1.5)

        # 스퀴즈 상태 (True=검은점/에너지 응축, False=회색점/변동성 폭발)
        df['squeeze_on'] = (df['bb_lower'] > df['kc_lower']) & (df['bb_upper'] < df['kc_upper'])

        highest = df['high'].rolling(window=20).max()
        lowest = df['low'].rolling(window=20).min()
        df['val'] = df['close'] - (((highest + lowest) / 2 + df['sma']) / 2)
        df['momentum'] = df['val'].rolling(window=20).mean()
        return df

    # --------------------------------------
    # [4. 리스크 관리 엔진]
    # --------------------------------------
    def calculate_position(self, entry_price, stop_loss):
        """포지션 크기 = 허용 손실액 ÷ (진입가 - 손절가)"""
        risk_per_unit = abs(entry_price - stop_loss)
        if risk_per_unit == 0:
            return 0.0

        target_max_loss = self.balance * MAX_RISK_PER_TRADE
        calculated_quantity = target_max_loss / risk_per_unit
        calculated_value = calculated_quantity * entry_price

        final_value = min(calculated_value, self.balance * 0.5)
        return final_value if final_value >= MIN_ORDER_VALUE else 0.0

    # --------------------------------------
    # [5. AI 트레이더 근거 평가 및 실행]
    # --------------------------------------
    def evaluate_and_execute(self, symbol):
        df = self.fetch_ohlcv(symbol, interval="15m")
        df = self.analyze_strategy_signals(df)
        if df.empty or len(df) < 2:
            return

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        squeeze_released = prev['squeeze_on'] and not curr['squeeze_on']
        momentum_bullish = (curr['momentum'] > 0) and (curr['momentum'] > prev['momentum'])

        # 근거 평가 생성
        eval_reason = "관망 (WAIT) - 스퀴즈 조건 미수렴"
        if prev['squeeze_on'] and curr['squeeze_on']:
            eval_reason = "에너지 응축 중 (스퀴즈 대기)"
        elif squeeze_released and not momentum_bullish:
            eval_reason = "변동성 폭발했으나 모멘텀 약함"
        elif squeeze_released and momentum_bullish:
            eval_reason = "🟢 매수 진입 조건 완벽 수렴"

        # 스캔 데이터 저장
        scan_item = {
            "symbol": symbol,
            "price": curr['close'],
            "squeeze_on": bool(curr['squeeze_on']),
            "momentum": round(float(curr['momentum']), 2),
            "eval_reason": eval_reason
        }
        
        # 스캔 목록 갱신
        self.scan_results = [item for item in self.scan_results if item['symbol'] != symbol]
        self.scan_results.append(scan_item)

        # 터미널 실시간 출력
        now_str = datetime.now().strftime('%H:%M:%S')
        print(f"[{now_str}] 🔍 [{symbol}] 현재가: {curr['close']:,.0f}원 | Squeeze: {curr['squeeze_on']} | Momentum: {curr['momentum']:.2f} | 평가: {eval_reason}")

        if self.active_position is not None:
            return

        if squeeze_released and momentum_bullish and self.btc_status != "DUMP":
            entry_price = curr['close'] * (1 + SLIPPAGE_RATE)
            stop_loss = df['low'].iloc[-5:].min()
            
            position_size = self.calculate_position(entry_price, stop_loss)
            if position_size > 0:
                take_profit = entry_price + ((entry_price - stop_loss) * 2.0)

                self.active_position = {
                    "symbol": symbol,
                    "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_price": entry_price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "quantity": position_size / entry_price,
                    "value": position_size,
                    "reason": "스퀴즈 해제 및 모멘텀 돌파"
                }
                print(f"[{now_str}] 🟢 [BUY Execution] {symbol} | 진입가: {entry_price:,.1f}원 | 손절가: {stop_loss:,.1f}원 | 익절가: {take_profit:,.1f}원")

    # --------------------------------------
    # [6. 실시간 청산 감시 및 매매일지]
    # --------------------------------------
    def monitor_active_position(self):
        if self.active_position is None:
            return

        pos = self.active_position
        df = self.fetch_ohlcv(pos['symbol'], interval="1m", count=2)
        if df.empty:
            return

        curr_price = df.iloc[-1]['close']
        now_str = datetime.now().strftime('%H:%M:%S')
        print(f"[{now_str}] 📈 [Holding {pos['symbol']}] 현재가: {curr_price:,.0f}원 | 손절가: {pos['stop_loss']:,.0f}원 | 익절가: {pos['take_profit']:,.0f}원")

        if curr_price <= pos['stop_loss']:
            self._close_position(curr_price, "STOP_LOSS")
        elif curr_price >= pos['take_profit']:
            self._close_position(curr_price, "TAKE_PROFIT")

    def _close_position(self, exit_price, reason):
        pos = self.active_position
        exit_p = exit_price * (1 - SLIPPAGE_RATE)
        pnl = (exit_p - pos['entry_price']) * pos['quantity']
        pnl_pct = ((exit_p / pos['entry_price']) - 1) * 100

        self.balance += pnl
        record = {
            "symbol": pos['symbol'],
            "entry_time": pos['entry_time'],
            "entry_price": pos['entry_price'],
            "stop_loss": pos['stop_loss'],
            "take_profit": pos['take_profit'],
            "exit_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "exit_price": exit_p,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "reason": reason,
            "balance_after": round(self.balance, 2)
        }
        self.journal.append(record)
        pd.DataFrame(self.journal).to_csv("logs/trade_journal.csv", index=False)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔴 [SELL Execution] {pos['symbol']} | {reason} | PnL: {pnl:+,.0f}원 ({pnl_pct:+.2f}%)")
        self.active_position = None

    def update_btc_macro(self):
        df = self.fetch_ohlcv("BTC_KRW", interval="1h", count=24)
        if not df.empty:
            self.btc_change = round(((df.iloc[-1]['close'] / df.iloc[0]['close']) - 1) * 100, 2)
            self.btc_status = "DUMP" if self.btc_change < -3.0 else "STABLE"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 [BTC Macro] 상태: {self.btc_status} (24H 변동률: {self.btc_change:+.2f}%)")

    def dump_status_json(self):
        """대시보드 동기화용 JSON 파일 내보내기"""
        status_data = {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "btc_status": self.btc_status,
            "btc_change": self.btc_change,
            "balance": self.balance,
            "binance_oi": self.binance_oi,
            "active_position": self.active_position,
            "scan_results": self.scan_results
        }
        with open("logs/status.json", "w", encoding="utf-8") as f:
            json.dump(status_data, f, ensure_ascii=False, indent=2)

# ==========================================
# 3. 비동기 스케줄러 및 실행
# ==========================================
bot = EYETRADER()

async def task_1min():
    while True:
        bot.monitor_active_position()
        bot.dump_status_json()
        await asyncio.sleep(10) # 10초마다 상태 JSON 업데이트

async def task_15min():
    while True:
        print(f"\n--- [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Target Altcoin Scanning ---")
        bot.fetch_binance_oi()
        for symbol in TARGET_ALTS:
            bot.evaluate_and_execute(symbol)
            bot.dump_status_json()
            await asyncio.sleep(1)
        await asyncio.sleep(300) # 5분마다 스캔 반복

async def task_1hour():
    while True:
        bot.update_btc_macro()
        await asyncio.sleep(3600)

async def main():
    print("🚀 EYE.py Professional AI Trader Pipeline Started...")
    bot.update_btc_macro()
    bot.fetch_binance_oi()
    await asyncio.gather(
        task_1min(),
        task_15min(),
        task_1hour()
    )

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    asyncio.run(main())

