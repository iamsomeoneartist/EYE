import time
from datetime import datetime
import numpy as np
import pandas as pd
import requests
import streamlit as st

# ==========================================
# 1. 설정 및 파라미터 (Settings & Config)
# ==========================================
INITIAL_SEED = 100000.0        # 시드머니: 10만원
MAX_RISK_PER_TRADE = 0.015     # 1회 최대 허용 손실율: 1.5% (1,500원)
MIN_ORDER_VALUE = 5000.0       # 빗썸 최소 주문금액
SLIPPAGE_RATE = 0.001          # 페이퍼 트레이딩 슬리피지 (0.1%)

st.set_page_config(page_title="AI 코인 트레이더 대시보드", layout="wide")

# ==========================================
# 2. 데이터 수집 및 지표 엔진 (Data & Strategy)
# ==========================================
def fetch_bithumb_ohlcv(symbol="BTC_KRW", interval="15m", max_count=50):
    """빗썸 REST API를 이용한 캔들 수집"""
    try:
        url = f"https://api.bithumb.com/public/candlestick/{symbol}/{interval}"
        res = requests.get(url, timeout=5).json()
        if res.get("status") == "0000":
            data = res["data"][-max_count:]
            df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume"])
            df["time"] = pd.to_datetime(df["time"].astype(int), unit="ms")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            return df
    except Exception as e:
        st.error(f"API 수집 에러 ({symbol}): {e}")
    return pd.DataFrame()

def calculate_squeeze_momentum(df, bb_len=20, bb_mult=2.0, kc_len=20, kc_mult=1.5):
    """Squeeze Momentum Indicator (LazyBear) 수식 계산"""
    if df.empty or len(df) < kc_len:
        return df

    # 볼린저 밴드
    df['sma'] = df['close'].rolling(window=bb_len).mean()
    df['std'] = df['close'].rolling(window=bb_len).std()
    df['bb_upper'] = df['sma'] + (bb_mult * df['std'])
    df['bb_lower'] = df['sma'] - (bb_mult * df['std'])

    # 켈트너 채널
    df['tr'] = np.maximum(df['high'] - df['low'], 
                          np.maximum(abs(df['high'] - df['close'].shift(1)), 
                                     abs(df['low'] - df['close'].shift(1))))
    df['atr'] = df['tr'].rolling(window=kc_len).mean()
    df['kc_upper'] = df['sma'] + (df['atr'] * kc_mult)
    df['kc_lower'] = df['sma'] - (df['atr'] * kc_mult)

    # 스퀴즈 상태 (검은 점: True / 회색 점: False)
    df['squeeze_on'] = (df['bb_lower'] > df['kc_lower']) & (df['bb_upper'] < df['kc_upper'])

    # 모멘텀
    highest = df['high'].rolling(window=kc_len).max()
    lowest = df['low'].rolling(window=kc_len).min()
    m1 = (highest + lowest) / 2
    df['val'] = df['close'] - ((m1 + df['sma']) / 2)
    df['momentum'] = df['val'].rolling(window=kc_len).mean()

    return df

# ==========================================
# 3. 리스크 및 시뮬레이션 세션 관리
# ==========================================
if 'balance' not in st.session_state:
    st.session_state.balance = INITIAL_SEED
if 'active_position' not in st.session_state:
    st.session_state.active_position = None
if 'journal' not in st.session_state:
    st.session_state.journal = []

def evaluate_and_trade(df, symbol):
    """지표 분석 및 리스크 계산 후 페이퍼 체결"""
    if df.empty or len(df) < 2:
        return "WAIT", "데이터 부족"

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    squeeze_released = prev['squeeze_on'] and not curr['squeeze_on']
    momentum_bullish = (curr['momentum'] > 0) and (curr['momentum'] > prev['momentum'])

    # 매수 신호 포착
    if squeeze_released and momentum_bullish:
        if st.session_state.active_position is not None:
            return "HOLD", "기존 포지션 유지 중"

        entry_price = curr['close'] * (1 + SLIPPAGE_RATE)
        stop_loss = df['low'].iloc[-5:].min()
        risk_per_share = entry_price - stop_loss

        if risk_per_share <= 0:
            return "WAIT", "손절가 설정 오류"

        # 리스크 엔진: 포지션 크기 = 허용 손실액 ÷ (진입가 - 손절가)
        target_loss = st.session_state.balance * MAX_RISK_PER_TRADE
        calc_qty = target_loss / risk_per_share
        calc_value = calc_qty * entry_price

        final_value = min(calc_value, st.session_state.balance * 0.5)
        if final_value < MIN_ORDER_VALUE:
            return "WAIT", "최소 주문금액 미달"

        take_profit = entry_price + (risk_per_share * 2.0) # R:R 1:2

        # 포지션 오픈
        st.session_state.active_position = {
            "symbol": symbol,
            "entry_time": datetime.now().strftime("%H:%M:%S"),
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "quantity": final_value / entry_price,
            "value": final_value
        }
        return "BUY", f"{symbol} 가상 매수 체결 ({entry_price:.1f}원)"

    # 포지션 감시 및 청산
    pos = st.session_state.active_position
    if pos and pos['symbol'] == symbol:
        curr_price = curr['close']
        if curr_price <= pos['stop_loss']:
            return close_position(curr_price, "STOP_LOSS")
        elif curr_price >= pos['take_profit']:
            return close_position(curr_price, "TAKE_PROFIT")

    return "WAIT", "신호 대기 중"

def close_position(exit_price, reason):
    pos = st.session_state.active_position
    exit_p = exit_price * (1 - SLIPPAGE_RATE)
    pnl = (exit_p - pos['entry_price']) * pos['quantity']
    pnl_pct = ((exit_p / pos['entry_price']) - 1) * 100

    st.session_state.balance += pnl
    st.session_state.journal.append({
        "시간": datetime.now().strftime("%H:%M:%S"),
        "종목": pos['symbol'],
        "진입가": round(pos['entry_price'], 1),
        "청산가": round(exit_p, 1),
        "손익(원)": round(pnl),
        "수익률(%)": round(pnl_pct, 2),
        "사유": reason
    })
    st.session_state.active_position = None
    return "SELL", f"포지션 청산 완료 ({reason})"

# ==========================================
# 4. Streamlit UI 대시보드
# ==========================================
st.title("🤖 AI 코인 트레이더 대시보드 (Paper Trading)")

# 상단 메트릭
col1, col2, col3 = st.columns(3)
col1.metric("가상 시드 잔고", f"{st.session_state.balance:,.0f} 원")
col2.metric("활성 포지션", "1개" if st.session_state.active_position else "없음")
col3.metric("누적 거래 횟수", f"{len(st.session_state.journal)}회")

st.markdown("---")

target_symbol = st.selectbox("감시 종목 선택 (빗썸 원화마켓)", ["XRP_KRW", "ITH_KRW", "SOL_KRW", "BTC_KRW"])

if st.button("실시간 시장 데이터 갱신 및 신호 분석"):
    df = fetch_bithumb_ohlcv(target_symbol)
    df = calculate_squeeze_momentum(df)
    action, msg = evaluate_and_trade(df, target_symbol)

    st.subheader(f"📊 {target_symbol} 상태 및 분석결과")
    st.info(f"**AI 판단:** {action} - {msg}")

    if not df.empty:
        curr_row = df.iloc[-1]
        c1, c2, c3 = st.columns(3)
        c1.metric("현재가", f"{curr_row['close']:,} 원")
        c2.metric("스퀴즈 상태", "에너지 응축 (ON)" if curr_row['squeeze_on'] else "변동성 폭발 (OFF)")
        c3.metric("모멘텀 지표", f"{curr_row['momentum']:.4f}")

st.markdown("---")
st.subheader("📜 페이퍼 트레이딩 매매일지")
if st.session_state.journal:
    st.dataframe(pd.DataFrame(st.session_state.journal), use_container_width=True)
else:
    st.write("아직 기록된 매매 내역이 없습니다.")
