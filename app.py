import asyncio
import os
import threading
import time
from datetime import datetime
from flask import Flask
import numpy as np
import pandas as pd
import requests

# ==========================================
# 1. UptimeRobot 전용 헬스체크 웹 서버
# ==========================================
web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    """UptimeRobot이 핑(Ping)을 보낼 웹 엔드포인트"""
    return f"EYE System Active! Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 200

def run_flask():
    """웹 서버 스레드 실행"""
    web_app.run(host='0.0.0.0', port=8080)

# ==========================================
# 2. 시스템 환경 및 리스크 파라미터 (Notebook 원칙)
# ==========================================
INITIAL_SEED = 100000.0        # 시드머니: 10만원
MAX_RISK_PER_TRADE = 0.015     # 1회 최대 허용 손실률: 1.5% (1,500원)
MIN_ORDER_VALUE = 5000.0       # 빗썸 최소 주문금액 보정
SLIPPAGE_RATE = 0.001          # 가상 슬리피지 (0.1%)
TARGET_ALTS = ["XRP_KRW", "ITH_KRW", "SOL_KRW", "DOGE_KRW"]

class EYETRADER:
    def __init__(self):
        self.balance = INITIAL_SEED
        self.active_position = None
        self.btc_status = "STABLE"
        self.journal = []

    # --------------------------------------
    # [Notebook 1. 시장 데이터 수집 모듈]
    # --------------------------------------
    def fetch_ohlcv(self, symbol, interval="15m", count=50):
        """빗썸 가격, 캔들, 거래량 수집"""
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
            print(f"[{datetime.now()}] [ERR] 데이터 수집 실패 ({symbol}): {e}")
        return pd.DataFrame()

    def fetch_binance_oi(self):
        """바이낸스 선물 미결제약정(OI) 수집 (파생상품 데이터)"""
        try:
            url = "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
            res = requests.get(url, timeout=3).json()
            return float(res.get("openInterest", 0))
        except:
            return 0.0

    # --------------------------------------
    # [Notebook 2 & 3. 시장 분석 및 진입 신호 엔진]
    # --------------------------------------
    def analyze_strategy_signals(self, df):
        """스퀴즈 모멘텀 지표 (변동성, 모멘텀, 지지/저항) 계산"""
        if df.empty or len(df) < 20:
            return df
            
        # 1. 볼린저 밴드 & 켈트너 채널
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

        # 모멘텀 히스토그램
        highest = df['high'].rolling(window=20).max()
        lowest = df['low'].rolling(window=20).min()
        df['val'] = df['close'] - (((highest + lowest) / 2 + df['sma']) / 2)
        df['momentum'] = df['val'].rolling(window=20).mean()
        return df

    # --------------------------------------
    # [Notebook 4. 리스크 관리 엔진]
    # --------------------------------------
    def calculate_position(self, entry_price, stop_loss):
        """포지션 크기 = 허용 손실액 ÷ (진입가 - 손절가)"""
        risk_per_unit = abs(entry_price - stop_loss)
        if risk_per_unit == 0:
            return 0.0

        target_max_loss = self.balance * MAX_RISK_PER_TRADE  # 허용 손실액
        calculated_quantity = target_max_loss / risk_per_unit
        calculated_value = calculated_quantity * entry_price

        # 최대 시드의 50% 제한 및 최소 주문금액 충족 보정
        final_value = min(calculated_value, self.balance * 0.5)
        return final_value if final_value >= MIN_ORDER_VALUE else 0.0

    # --------------------------------------
    # [Notebook 5. AI 트레이더 근거 평가 및 실행]
    # --------------------------------------
    def evaluate_and_execute(self, symbol):
        """신호/리스크 검증 후 매수 조건 수렴 시 자동 실행"""
        if self.active_position is not None:
            return

        df = self.fetch_ohlcv(symbol, interval="15m")
        df = self.analyze_strategy_signals(df)
        if df.empty or len(df) < 2:
            return

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        # 조건 1: BTC 급락(DUMP) 상태 제외
        # 조건 2: 스퀴즈 해제 (에너지 응축 후 변동성 폭발)
        # 조건 3: 모멘텀 0선 위 상향 확장
        squeeze_released = prev['squeeze_on'] and not curr['squeeze_on']
        momentum_bullish = (curr['momentum'] > 0) and (curr['momentum'] > prev['momentum'])

        if squeeze_released and momentum_bullish and self.btc_status != "DUMP":
            entry_price = curr['close'] * (1 + SLIPPAGE_RATE)
            stop_loss = df['low'].iloc[-5:].min()  # 최근 5개 봉 최저가 손절
            
            position_size = self.calculate_position(entry_price, stop_loss)
            if position_size > 0:
                take_profit = entry_price + ((entry_price - stop_loss) * 2.0)  # 손익비(R:R) 1:2

                self.active_position = {
                    "symbol": symbol,
                    "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_price": entry_price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "quantity": position_size / entry_price,
                    "value": position_size,
                    "reason": "스퀴즈 해제 + 0선 상향 모멘텀 폭발"
                }
                print(f"[{datetime.now()}] 🟢 [BUY] {symbol} | 진입가: {entry_price:,.1f}원 | 손절가: {stop_loss:,.1f}원 | 익절가: {take_profit:,.1f}원 | 포지션: {position_size:,.0f}원")

    # --------------------------------------
    # 실시간 모니터링 및 [Notebook 6. 매매일지]
    # --------------------------------------
    def monitor_active_position(self):
        """1분 루프: 활성 포지션 실시간 감지 및 자동 청산"""
        if self.active_position is None:
            return

        pos = self.active_position
        df = self.fetch_ohlcv(pos['symbol'], interval="1m", count=2)
        if df.empty:
            return

        curr_price = df.iloc[-1]['close']

        if curr_price <= pos['stop_loss']:
            self._close_position(curr_price, "STOP_LOSS (손절)")
        elif curr_price >= pos['take_profit']:
            self._close_position(curr_price, "TAKE_PROFIT (익절)")

    def _close_position(self, exit_price, reason):
        """포지션 청산 및 매매일지 자동 저장"""
        pos = self.active_position
        exit_p = exit_price * (1 - SLIPPAGE_RATE)
        pnl = (exit_p - pos['entry_price']) * pos['quantity']
        pnl_pct = ((exit_p / pos['entry_price']) - 1) * 100

        self.balance += pnl
        record = {
            **pos,
            "exit_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "exit_price": exit_p,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "reason": reason,
            "balance_after": round(self.balance, 2)
        }
        self.journal.append(record)
        print(f"[{datetime.now()}] 🔴 [SELL] {pos['symbol']} | 사유: {reason} | 손익: {pnl:+,.0f}원 ({pnl_pct:+.2f}%) | 잔고: {self.balance:,.0f}원")
        self.active_position = None

    def update_btc_macro(self):
        """1시간 루프: BTC 추세 감시"""
        df = self.fetch_ohlcv("BTC_KRW", interval="1h", count=24)
        if not df.empty:
            price_change = ((df.iloc[-1]['close'] / df.iloc[0]['close']) - 1) * 100
            self.btc_status = "DUMP" if price_change < -3.0 else "STABLE"
            print(f"[{datetime.now()}] 🔍 [BTC Macro] 추세 상태: {self.btc_status} (24H 변동률: {price_change:+.2f}%)")

# ==========================================
# 3. 24시간 비동기 태스크 스케줄러
# ==========================================
bot = EYETRADER()

async def task_1min():
    """1분 주기: 포지션 실시간 손절/익절 감시"""
    while True:
        bot.monitor_active_position()
        await asyncio.sleep(60)

async def task_15min():
    """15분 주기: 스퀴즈 모멘텀 조건 수렴 스캐닝"""
    while True:
        for symbol in TARGET_ALTS:
            bot.evaluate_and_execute(symbol)
            await asyncio.sleep(1)
        await asyncio.sleep(900)

async def task_1hour():
    """1시간 주기: BTC 추세 업데이트 및 매매일지 백업"""
    while True:
        bot.update_btc_macro()
        if bot.journal:
            os.makedirs("logs", exist_ok=True)
            pd.DataFrame(bot.journal).to_csv("logs/trade_journal.csv", index=False)
        await asyncio.sleep(3600)

async def main():
    print("🚀 EYE.py - 24시간 실시간 무인 코인 AI 트레이더 가동...")
    bot.update_btc_macro()
    await asyncio.gather(
        task_1min(),
        task_15min(),
        task_1hour()
    )

if __name__ == "__main__":
    # UptimeRobot 감시용 Flask 서버 스레드 시작
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # 메인 비동기 루프 실행
    asyncio.run(main())

