#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
ib_intraday_15m_strategy_auto_pos.py

功能总览：
- 自动识别当前是否为「美股盘中」：
    * 使用本机北京时间 -> 转换为美东时间（America/New_York）
    * 若为美股 RTH 09:30–16:00 且为工作日 -> 盘中模式 intraday
    * 其他时间 -> 日线模式 daily
- 自动从 IB 读取当前真实持仓（股票）及 avgCost
- 若为盘中模式 intraday：
    * 为每只持仓请求：
        - 最近 10 天 15 分钟 K
        - 最近 10 天 1 小时 K
    * 分别计算 15m / 1h 的 B / C / D 信号
    * 基于当日 15m K 计算 VWAP（成交量加权平均价）
    * 生成综合盘中策略建议（含 15m/1h + VWAP 解读）
- 若为日线模式 daily：
    * 为每只持仓请求最近 1 年日线 K
    * 统一调用 indicator_rules.calc_tech_indicators / classify_bcd_signal
    * 结合 B / C / D + EMA 结构生成中长线策略
- 所有模式下：
    * 使用 avgCost 计算浮盈/亏 P&L%
    * 基于 avgCost + 最近一段时间高低点 + EMA20
      自动生成「关键价格带」：
        - 防守价带：三条递进的防守 / 止损线
        - 进攻价带：四条递进的加仓 / 止盈线
      （综合了百分比、斐波那契比例和支撑/压力位）
    * 输出主表（Symbol / Signal / P&L / Last / Cost / EMA20 / VOL20 / Action）
"""

# ====== 交易 / 模拟配置 ======
SIMULATE_ONLY = True          # ★ 只读模式：True=只打印计划，不真的 placeOrder
ACCOUNT_EQUITY_MANUAL = 30000.0   # 先手动填账户权益，美金
RISK_PER_TRADE_PCT = 0.005        # 单笔最大风险 0.5%

import time
import threading
import os
import csv
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.common import BarData

from indicator_rules import calc_tech_indicators, classify_bcd_signal, passes_long_entry_filter
from price_bands_engine import (
    generate_price_bands,
    PriceBandsContext,
    PositionInfo,
    TechnicalLevels,
)
from order_plan_engine import build_order_plan_from_bands, print_order_plan
from price_bands_engine import generate_price_bands, PriceBandsResult
from order_plan_engine import build_order_plan_from_bands, OrderPlan, OrderLeg

def _infer_mode_from_daily_info(info: Dict) -> str:
    """
    根据日线分析结果，粗略推断一个交易模式字符串，先作为标签用：
      - "strong_trend_pullback_long" : 强趋势回调做多
      - "range_buy_the_dip_long"     : 盘整区间低吸
      - "bear_rally_long"            : 空头趋势中的反弹做多
      - "recommendation_follow"      : 推荐/题材驱动模式（后面再用）
    目前只看两个字段：
      - info["trend_desc"]  类似 '多头排列' / '空头排列' / '均线纠缠'
      - info["signal_grade"] 类似 'B' / 'C' / 'D'
    """
    trend = (info.get("trend_desc") or "").strip()
    grade = (info.get("signal_grade") or "").strip().upper()

    # 非常粗的第一版映射，后面可以再调：
    if "多头排列" in trend:
        # 多头趋势里：
        #   B 视为“强趋势回调做多”
        #   C 也算强趋势，但略保守
        if grade in ("A", "B", "C"):
            return "strong_trend_pullback_long"
        else:
            return "range_buy_the_dip_long"

    if "空头排列" in trend:
        # 空头里，只做“反弹”而不是追涨
        return "bear_rally_long"

    if "纠缠" in trend or "震荡" in trend:
        # 均线纠缠 / 震荡 → 盘整低吸
        return "range_buy_the_dip_long"

    # 兜底：先用强趋势回调
    return "strong_trend_pullback_long"


# ========= 时间 / 模式判断 =========

def is_us_market_open_now() -> bool:
    """
    使用本机北京时间判断当前是否为美股盘中：
    - 将 Asia/Shanghai 时间转换为 America/New_York
    - 周一 ~ 周五
    - 09:30 <= 美东时间 <= 16:00
    """
    cn_tz = ZoneInfo("Asia/Shanghai")
    ny_tz = ZoneInfo("America/New_York")

    now_cn = datetime.now(cn_tz)
    now_ny = now_cn.astimezone(ny_tz)

    # 周一=0, 周日=6
    if now_ny.weekday() >= 5:
        return False

    market_open = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_ny <= market_close


# ========= 合约帮助函数 =========

def stock_contract(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD"
) -> Contract:
    c = Contract()
    c.symbol = symbol
    c.secType = "STK"
    c.exchange = exchange
    c.currency = currency
    return c


# ========= 旧版 B / C / D 逻辑（盘中暂时继续沿用，日线改用 indicator_rules） =========

def generate_bcd_signal(symbol: str, bars: List[BarData]):
    """
    简化版 B / C / D 信号，用于盘中；日线已用 indicator_rules 替代。
    """
    if len(bars) < 5:
        return "C", {"reason": "bar 数太少，自动观望"}

    # 最近最多 25 根
    last_n = bars[-25:] if len(bars) > 25 else bars
    closes = [b.close for b in last_n]
    vols = [b.volume for b in last_n]

    if len(closes) < 3:
        return "C", {"reason": "有效 bar 少于 3 根，自动观望"}

    ma_window = min(20, len(closes))
    ma20 = sum(closes[-ma_window:]) / ma_window
    vol20 = sum(vols[-ma_window:]) / ma_window

    last_bar = last_n[-1]
    prev_bar = last_n[-2]

    last_close = last_bar.close
    last_vol = last_bar.volume
    prev_close = prev_bar.close

    def rnd(x):
        return round(x, 4)

    info = {
        "symbol": symbol,
        "last_time": last_bar.date,
        "last_close": rnd(last_close),
        "last_vol": int(last_vol),
        "ma20": rnd(ma20),
        "vol20": int(vol20),
    }

    up_threshold = 0.002      # 0.2% 上穿 MA20 视为偏多
    down_threshold = 0.002    # 0.2% 下破 MA20 视为偏空
    vol_mult = 1.3            # 放量倍数阈值

    price_above_ma = last_close > ma20 * (1 + up_threshold)
    price_below_ma = last_close < ma20 * (1 - down_threshold)
    price_up = last_close > prev_close
    price_down = last_close < prev_close
    volume_heavy = last_vol >= vol20 * vol_mult

    # B 信号：价强 + 放量 + 在均线上方
    if price_above_ma and price_up and volume_heavy:
        info["reason"] = "价在 MA20 上方、放量上涨 → 偏多 B 信号"
        return "B", info

    # D 信号：价弱 + 放量 + 在均线下方
    if price_below_ma and price_down and volume_heavy:
        info["reason"] = "价在 MA20 下方、放量下跌 → 防守 D 信号"
        return "D", info

    # 其他情况 → C
    info["reason"] = "未出现明显放量突破 / 跌破 → 中性 C"
    return "C", info


def calc_pnl_pct(last_close: float, cost: Optional[float]) -> Optional[float]:
    """根据 avgCost 计算浮盈/亏百分比"""
    if cost is None or cost <= 0:
        return None
    return (last_close / cost - 1.0) * 100.0


def decide_action(signal: str, pnl_pct: Optional[float]) -> str:
    """根据信号 + 浮盈/亏给出一句话操作建议"""
    if pnl_pct is None:
        if signal == "B":
            return "偏多信号，可结合仓位与大盘酌情加仓或持有"
        if signal == "D":
            return "防守信号，考虑减仓或设 tighter 止损"
        return "信号中性，观望为主"

    p = round(pnl_pct, 2)

    if signal == "B":
        if p <= -15:
            return "深度套牢 + 放量反弹，优先利用反弹减仓 / 降低仓位风险"
        if -15 < p <= -5:
            return "趋势转好但仍亏损，持有为主，可等待更强确认后再考虑加仓"
        if -5 < p < 10:
            return "小幅浮盈/亏，偏多下可小幅加仓或持有，注意整体仓位"
        return "盈利较多 + 偏多，可考虑分批止盈，同时保留部分主仓继续跟随"

    if signal == "D":
        if p <= -10:
            return "趋势偏弱且亏损较大，建议分批减仓或执行原定止损计划"
        if -10 < p < 0:
            return "轻度亏损 + 偏弱，收紧仓位，避免继续扩大亏损"
        if 0 <= p < 15:
            return "有盈利但出现防守信号，可考虑先锁定一部分利润"
        return "高位防守信号，建议分批止盈，避免回吐过大浮盈"

    # signal == "C"
    if abs(p) < 5:
        return "浮盈/亏不大且信号中性，观望为主，不急于操作"
    if p <= -5:
        return "中性信号 + 亏损，轻仓观察为主，不盲目补仓"
    return "中性信号 + 有盈利，可按计划逐步止盈或继续持有"


# ========= 最近高低点工具函数 =========

def get_recent_high_low(
    bars: List[BarData],
    max_lookback: int = 80
) -> Tuple[Optional[float], Optional[float]]:
    """
    从最近 max_lookback 根 K 线中，提取最近一段时间的最高价 / 最低价。
    - 对日线：大约 3~4 个月；
    - 对 15m / 1h：最近若干交易日的区间高低点。
    """
    if not bars:
        return None, None

    if len(bars) > max_lookback:
        sub = bars[-max_lookback:]
    else:
        sub = bars

    lows = [b.low for b in sub if getattr(b, "low", None) is not None]
    highs = [b.high for b in sub if getattr(b, "high", None) is not None]

    if not lows or not highs:
        return None, None

    return min(lows), max(highs)


# ========= 动态关键价格带（升级版） =========

def compute_price_bands(
    cost: Optional[float],
    last_price: Optional[float] = None,
    ema20: Optional[float] = None,
    recent_low: Optional[float] = None,
    recent_high: Optional[float] = None,
):
    """
    基于成本价 + 最近一段时间高低点 + EMA20 计算关键价格带。

    设计思路：
    1. 先生成一组“默认百分比价位”：
        - 防守：cost * (1 - 5%) / (1 - 10%) / (1 - 15%)
        - 进攻：cost * (1 + 5%) / (1 + 10%) / (1 + 15%) / (1 + 20%)
    2. 再把下面这些价位“加入候选池”：
        - 最近一段时间的 swing low / swing high
        - 低点 / 高点与成本之间的 38.2%、50%、61.8% 斐波那契分位
        - EMA20（在成本下方视为防守线，在成本上方视为压力/止盈线）
    3. 最后从所有候选价位中，自动挑选：
        - 离成本最近的三条“下方价位”作为防守带（按价格从低到高排序）
        - 离成本最近的四条“上方价位”作为进攻 & 止盈带（从低到高）

    这样生成的价位同时兼顾：
        - 固定百分比的风险/收益比例
        - 实际行情中的支撑/压力位置
        - 均线（EMA20）的趋势信息
    """
    if cost is None or cost <= 0:
        return None

    defense_candidates: List[float] = []
    offense_candidates: List[float] = []

    # 1) 默认百分比价位（保证至少有 3+4 条）
    for pct in (0.05, 0.10, 0.15):
        defense_candidates.append(cost * (1 - pct))
    for pct in (0.05, 0.10, 0.15, 0.20):
        offense_candidates.append(cost * (1 + pct))

    # 2) 最近高低点直接加入候选
    if recent_low is not None and recent_low > 0:
        defense_candidates.append(recent_low)
    if recent_high is not None and recent_high > 0:
        offense_candidates.append(recent_high)

    # 3) 斐波那契分位（在最近低点 / 高点 与 成本 之间）
    if recent_low is not None and recent_high is not None and recent_high > recent_low:
        # 向下：成本到 swing low 之间
        if cost > recent_low:
            down_range = cost - recent_low
            for ratio in (0.382, 0.5, 0.618):
                level = cost - down_range * ratio
                if level > 0:
                    defense_candidates.append(level)

        # 向上：成本到 swing high 之间
        if recent_high > cost:
            up_range = recent_high - cost
            for ratio in (0.382, 0.5, 0.618):
                level = cost + up_range * ratio
                if level > 0:
                    offense_candidates.append(level)
            # 顺便把 swing high 本身视作一个重要止盈位（若高点本来没加入，这里再加一遍无妨）
            offense_candidates.append(cost + up_range)

    # 4) EMA20 也作为候选支撑/压力
    if ema20 is not None and ema20 > 0:
        if ema20 < cost:
            defense_candidates.append(ema20)
        elif ema20 > cost:
            offense_candidates.append(ema20)

    # 5) 去重 + 排序 + 选出离成本最近的几条
    def _uniq_sorted(levels: List[float], reverse: bool = False) -> List[float]:
        # 先四舍五入到 4 位小数去重，再排序
        uniq = {round(x, 4) for x in levels if x is not None and x > 0}
        return sorted(uniq, reverse=reverse)

    # 下方价位：按价格从高到低排序，选出最靠近成本的几条，然后再按从低到高输出
    defense_all = [lv for lv in defense_candidates if lv < cost]
    defense_sorted_desc = _uniq_sorted(defense_all, reverse=True)
    defense_picked = defense_sorted_desc[:3]  # 离成本最近的三条
    defense = [round(x, 2) for x in sorted(defense_picked)]  # 输出时按价格从低到高

    # 上方价位：按价格从低到高排序，选出最靠近成本的几条
    offense_all = [lv for lv in offense_candidates if lv > cost]
    offense_sorted_asc = _uniq_sorted(offense_all, reverse=False)
    offense_picked = offense_sorted_asc[:4]  # 离成本最近的四条
    offense = [round(x, 2) for x in offense_picked]

    if not defense or not offense:
        return None

    return {"defense": defense, "offense": offense}


# ========= VWAP 计算（盘中） =========

def calc_today_vwap_from_15m(bars_15: List[BarData]) -> Optional[float]:
    """
    用 15m K 计算“当日 VWAP”：
    - 取 15m bars 中，日期 = 最近一根 bar 的日期（YYYYMMDD）
    - VWAP = sum(close * volume) / sum(volume)
    """
    if not bars_15:
        return None

    last_date_str = str(bars_15[-1].date)[:8]
    total_pv = 0.0
    total_vol = 0.0
    for b in bars_15:
        d = str(b.date)[:8]
        if d != last_date_str:
            continue
        v = b.volume
        if v is None or v <= 0:
            continue
        total_pv += b.close * v
        total_vol += v

    if total_vol <= 0:
        return None
    return round(total_pv / total_vol, 4)


# ========= 主应用类 =========

class Intraday15mStrategyAutoPosApp(EWrapper, EClient):
    def __init__(self, host: str, port: int, client_id: int, mode: str):
        """
        mode 参数说明：
          - "intraday": 盘中，使用 15m + 1h + VWAP
          - "daily":    非盘中，使用日线 + EMA/MACD/RSI/OBV
        """
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)

        self.host = host
        self.port = port
        self.client_id = client_id
        self.mode = mode

        self.connected_ok = False

        # 持仓：symbol -> avgCost / shares
        self.position_costs: Dict[str, float] = {}
        self.position_shares: Dict[str, float] = {}
        self.watchlist: List[str] = []

        # 历史数据相关
        self.req_id_base = 3000
        # reqId -> {"symbol": str, "tf": "15m"|"1h"|"1d"}
        self.reqid_map: Dict[int, Dict[str, str]] = {}
        # symbol -> 预期 timeframes
        self.expected_tfs: Dict[str, Set[str]] = {}
        # symbol -> 已完成 timeframes
        self.completed_tfs: Dict[str, Set[str]] = {}

        # K 线存储
        self.bars_15m: Dict[str, List[BarData]] = {}
        self.bars_1h: Dict[str, List[BarData]] = {}
        self.bars_1d: Dict[str, List[BarData]] = {}

        # 最终信号
        self.signals: Dict[str, Dict] = {}

        # 当前正在处理第几个 symbol
        self.current_index = 0

    # ----- 连接回调 -----

    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.connected_ok = True
        print(f"✅ 已连接到 IB Gateway，nextValidId = {orderId}")

        # 使用延迟行情模式
        self.reqMarketDataType(3)
        print("📊 已将行情模式设置为：延迟行情 (marketDataType = 3)")

        # 第一步：请求持仓
        print("📌 正在从 IB 读取当前持仓 (positions) ...")
        self.reqPositions()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # 特殊处理：code 200（No security definition），认为该 symbol 无法处理，直接跳过
        if errorCode == 200 and reqId in self.reqid_map:
            info_map = self.reqid_map.get(reqId, {})
            symbol = info_map.get("symbol")
            if symbol is not None:
                print(f"⚠ {symbol} 无法获取历史数据 (code 200: {errorString})，跳过该标的。")
                info = {
                    "symbol": symbol,
                    "signal": "-",
                    "last_close": 0.0,
                    "last_price": 0.0,
                    "last_vol": 0,
                    "ema20": 0.0,
                    "vol20": 0,
                    "cost": self.position_costs.get(symbol),
                    "pnl_pct": None,
                    "action": "无法获取历史数据（可能是结构性产品 / 债券 / 现金），本脚本只分析普通股票。",
                    "reason": "No security definition",
                }
                self.signals[symbol] = info

                self.completed_tfs.setdefault(symbol, set()).add(info_map.get("tf", "unknown"))
                self._try_advance_after_symbol(symbol)
                return

        prefix = "❌"
        if errorCode in (2103, 2104, 2106, 2107, 2158):
            prefix = "ℹ️"
        print(f"{prefix} Error. reqId={reqId}, code={errorCode}, msg={errorString}")

    # ----- 持仓回调 -----

    def position(self, account, contract, position, avgCost):
        if contract.secType != "STK":
            return
        if abs(position) < 1e-6:
            return

        symbol = contract.symbol
        self.position_costs[symbol] = avgCost
        self.position_shares[symbol] = position

    def positionEnd(self):
        self.watchlist = sorted(self.position_costs.keys())
        print(f"📌 持仓读取完毕，共 {len(self.watchlist)} 只股票。")

        if not self.watchlist:
            print("⚠ 当前账户没有股票持仓，本次无需生成策略。")
            self._disconnect_safely()
            return

        print("本次分析的股票：", ", ".join(self.watchlist))
        if self.mode == "intraday":
            print("📈 当前为美股盘中，将使用 15 分钟 + 1 小时 K + VWAP 进行盘中策略分析。")
        else:
            print("🌙 当前为非盘中，将使用日线 EMA / MACD / RSI / OBV 进行中长线策略分析。")

        self.current_index = 0
        self.request_next_symbol_history()

    # ----- 历史数据回调 -----

    def historicalData(self, reqId: int, bar: BarData):
        info_map = self.reqid_map.get(reqId)
        if not info_map:
            return
        symbol = info_map["symbol"]
        tf = info_map["tf"]

        if tf == "15m":
            self.bars_15m.setdefault(symbol, []).append(bar)
        elif tf == "1h":
            self.bars_1h.setdefault(symbol, []).append(bar)
        elif tf == "1d":
            self.bars_1d.setdefault(symbol, []).append(bar)

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        info_map = self.reqid_map.get(reqId)
        if not info_map:
            return
        symbol = info_map["symbol"]
        tf = info_map["tf"]

        print(f"📥 {symbol} {tf} K 数据接收完毕。")

        self.completed_tfs.setdefault(symbol, set()).add(tf)
        self._try_process_symbol(symbol)
        self._try_advance_after_symbol(symbol)

    # ----- 请求数据 & 状态管理 -----

    def request_next_symbol_history(self):
        if self.current_index >= len(self.watchlist):
            return

        symbol = self.watchlist[self.current_index]
        contract = stock_contract(symbol)

        if self.mode == "intraday":
            self.expected_tfs[symbol] = {"15m", "1h"}

            req_id_15m = self.req_id_base + self.current_index * 10 + 1
            self.reqid_map[req_id_15m] = {"symbol": symbol, "tf": "15m"}
            print(f"➡️ 正在请求 {symbol} 最近 10 天的 15 分钟 K ...")
            self.reqHistoricalData(
                reqId=req_id_15m,
                contract=contract,
                endDateTime="",
                durationStr="10 D",      # 从 5 D 改成 10 D
                barSizeSetting="15 mins",
                whatToShow="TRADES",
                useRTH=1,
                formatDate=1,
                keepUpToDate=False,
                chartOptions=[]
            )

            req_id_1h = self.req_id_base + self.current_index * 10 + 2
            self.reqid_map[req_id_1h] = {"symbol": symbol, "tf": "1h"}
            print(f"➡️ 正在请求 {symbol} 最近 10 天的 1 小时 K ...")
            self.reqHistoricalData(
                reqId=req_id_1h,
                contract=contract,
                endDateTime="",
                durationStr="10 D",
                barSizeSetting="1 hour",
                whatToShow="TRADES",
                useRTH=1,
                formatDate=1,
                keepUpToDate=False,
                chartOptions=[]
            )
        else:
            self.expected_tfs[symbol] = {"1d"}

            req_id_1d = self.req_id_base + self.current_index * 10 + 9
            self.reqid_map[req_id_1d] = {"symbol": symbol, "tf": "1d"}
            print(f"➡️ 正在请求 {symbol} 最近 1 年的 日线 K ...")
            self.reqHistoricalData(
                reqId=req_id_1d,
                contract=contract,
                endDateTime="",
                durationStr="1 Y",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=1,
                formatDate=1,
                keepUpToDate=False,
                chartOptions=[]
            )

    def _try_process_symbol(self, symbol: str):
        if symbol in self.signals:
            return

        expected = self.expected_tfs.get(symbol)
        done = self.completed_tfs.get(symbol, set())
        if not expected or not expected.issubset(done):
            return

        if self.mode == "intraday":
            self._process_intraday_symbol(symbol)
        else:
            self._process_daily_symbol(symbol)

    def _try_advance_after_symbol(self, symbol: str):
        expected = self.expected_tfs.get(symbol)
        done = self.completed_tfs.get(symbol, set())
        if not expected or not expected.issubset(done):
            return

        if self.current_index < len(self.watchlist) and self.watchlist[self.current_index] == symbol:
            self.current_index += 1
            if self.current_index < len(self.watchlist):
                time.sleep(1.0)
                self.request_next_symbol_history()
            else:
                print("\n✅ 所有股票信号计算完成：\n")
                self.print_summary()
                self.save_csv_report()
                self.print_detailed_strategies()
                threading.Timer(2.0, self._disconnect_safely).start()

    # ----- 各模式下的信号生成 -----

    def _process_intraday_symbol(self, symbol: str):
        bars_15 = self.bars_15m.get(symbol, [])
        bars_1h = self.bars_1h.get(symbol, [])

        if not bars_15 and not bars_1h:
            return

        sig_15, info_15 = generate_bcd_signal(symbol, bars_15) if bars_15 else ("C", {"reason": "无 15m 数据"})
        sig_1h, info_1h = generate_bcd_signal(symbol, bars_1h) if bars_1h else ("C", {"reason": "无 1h 数据"})

        vwap = calc_today_vwap_from_15m(bars_15) if bars_15 else None

        combined_signal, intraday_comment = self._combine_intraday_signals(sig_15, sig_1h)

        last_close = info_15.get("last_close")
        if vwap is not None and last_close is not None:
            up_thr = 0.002
            down_thr = 0.002
            if last_close > vwap * (1 + up_thr):
                intraday_comment += f" 当前价在 VWAP({vwap}) 上方，说明今日整体资金偏多。"
            elif last_close < vwap * (1 - down_thr):
                intraday_comment += f" 当前价在 VWAP({vwap}) 下方，说明今日整体资金偏弱。"
            else:
                intraday_comment += f" 当前价在 VWAP({vwap}) 附近，买卖力量较均衡。"

        info = dict(info_15)
        info["signal"] = combined_signal
        info["signal_15m"] = sig_15
        info["signal_1h"] = sig_1h
        info["reason_15m"] = info_15.get("reason")
        info["reason_1h"] = info_1h.get("reason")
        info["intraday_comment"] = intraday_comment
        info["vwap"] = vwap

        cost = self.position_costs.get(symbol)
        pnl_pct = calc_pnl_pct(info["last_close"], cost)

        info["cost"] = round(cost, 4) if cost is not None else None
        info["pnl_pct"] = round(pnl_pct, 2) if pnl_pct is not None else None
        info["action"] = decide_action(combined_signal, pnl_pct)

        # 最近高低点：盘中用 1h 优先，没有就退回 15m
        recent_low, recent_high = (None, None)
        if bars_1h:
            recent_low, recent_high = get_recent_high_low(bars_1h, max_lookback=80)
        elif bars_15:
            recent_low, recent_high = get_recent_high_low(bars_15, max_lookback=80)

        # 动态价带（升级版）
        bands = compute_price_bands(
            cost=cost,
            last_price=info.get("last_close"),
            ema20=info.get("ma20"),
            recent_low=recent_low,
            recent_high=recent_high,
        )
        if bands:
            info["bands_defense"] = bands["defense"]
            info["bands_offense"] = bands["offense"]

        # 为了和日线保持统一字段名字，这里也补上 ema20 / vol20（用 ma20 / vol20）
        info["ema20"] = info.get("ma20")
        info["vol20"] = info.get("vol20")

        # 当前价字段：如果以后接入实时价，可覆盖 last_price
        info["last_price"] = info.get("last_close")

        self.signals[symbol] = info

    def _combine_intraday_signals(self, sig_15: str, sig_1h: str):
        if sig_15 == sig_1h:
            if sig_15 == "B":
                return "B", "15m 与 1h 均为 B，短线与小时级别趋势一致偏多。"
            if sig_15 == "D":
                return "D", "15m 与 1h 均为 D，短线与小时级别趋势一致偏弱。"
            return "C", "15m 与 1h 均为 C，整体偏中性，观望为主。"

        if sig_15 == "B" and sig_1h == "C":
            return "B", "15m 偏多、1h 中性 → 短线反弹，趋势待确认。"
        if sig_15 == "C" and sig_1h == "B":
            return "B", "1h 偏多、15m 回调 → 上升趋势中的短线震荡。"
        if sig_15 == "D" and sig_1h == "C":
            return "D", "15m 偏弱、1h 中性 → 短线走弱，适度防守。"
        if sig_15 == "C" and sig_1h == "D":
            return "D", "1h 偏弱、15m 反弹 → 反弹中的下跌趋势，以减仓为主。"
        if sig_15 == "B" and sig_1h == "D":
            return "C", "15m 偏多但 1h 偏空 → 反弹中的下降趋势，谨慎参与。"
        if sig_15 == "D" and sig_1h == "B":
            return "C", "1h 偏多但 15m 回调 → 上升趋势中的回调，等待企稳后再考虑加仓。"

        return "C", "15m 与 1h 信号分化，整体以中性观望处理。"

    def _process_daily_symbol(self, symbol: str):
        """
        日线模式：完全使用 indicator_rules 统一指标 + B/C/D 逻辑。
        同时把一部分技术位保存到 info 里，后面通过 ctx.tech 传给价格带引擎。
        """
        bars = self.bars_1d.get(symbol, [])
        if not bars:
            return

        # 1) 计算技术指标
        indicators = calc_tech_indicators(bars, mode="daily")
        if not indicators:
            return

        signal, tech_comment = classify_bcd_signal(indicators, mode="daily")

        # 统一使用 last_close；如未来加 'close' 字段也能兼容
        last_close = indicators.get("last_close")
        if last_close is None:
            last_close = indicators.get("close")

        ema20 = indicators.get("ema20")
        ema50 = indicators.get("ema50")
        ema200 = indicators.get("ema200")
        vol20 = indicators.get("vol20")

        shares = self.position_shares.get(symbol)

        # 均线趋势描述（保持和之前版本风格一致）
        trend_desc = "均线纠缠（震荡）"
        if all(x is not None for x in (ema20, ema50, ema200)):
            if ema20 > ema50 > ema200:
                trend_desc = "多头排列（中长期上升趋势）"
            elif ema20 < ema50 < ema200:
                trend_desc = "空头排列（中长期下跌趋势）"

        pos_value = None
        if shares is not None and last_close is not None:
            pos_value = round(last_close * shares, 2)

        info: Dict[str, object] = {
            "symbol": symbol,
            "signal": signal,
            "last_close": round(last_close, 4) if last_close is not None else None,
            # 当前价：目前用 last_close；将来如果有实时价，可以单独覆盖 last_price
            "last_price": round(last_close, 4) if last_close is not None else None,
            "ema20": round(ema20, 4) if ema20 is not None else None,
            "ma20": round(ema20, 4) if ema20 is not None else None,  # 向下兼容旧字段
            "ma50": round(ema50, 4) if ema50 is not None else None,
            "ma200": round(ema200, 4) if ema200 is not None else None,
            "vol20": int(vol20) if vol20 is not None else None,
            "daily_trend": trend_desc,
            "tech_comment": tech_comment,
            "shares": shares,
            "position_value": pos_value,
        }

        # 2) P&L、成本、Action
        cost = self.position_costs.get(symbol)
        pnl_pct = calc_pnl_pct(last_close, cost) if last_close is not None else None
        info["cost"] = round(cost, 4) if cost is not None else None
        info["pnl_pct"] = round(pnl_pct, 2) if pnl_pct is not None else None

        base_action = decide_action(signal, pnl_pct)
        info["action"] = f"{base_action} （日线趋势：{trend_desc}；技术面：{tech_comment}）"

        # 3) 关键价格带：日线直接用日线 K 的高低点
        recent_low, recent_high = get_recent_high_low(bars, max_lookback=80)
        bands = compute_price_bands(
            cost=cost,
            last_price=last_close,
            ema20=ema20,
            recent_low=recent_low,
            recent_high=recent_high,
        )
        if bands:
            info["bands_defense"] = bands["defense"]
            info["bands_offense"] = bands["offense"]

        # 4) 为价格带引擎准备一些“技术位”，后面会放到 ctx.tech 里做多重共振
        #    Fib：用最近 80 根 K 的区间高低点做 38.2 / 50 / 61.8
        fib_levels: List[float] = []
        if recent_low is not None and recent_high is not None and recent_high > recent_low:
            rng = recent_high - recent_low
            for r in (0.382, 0.5, 0.618):
                level = recent_low + rng * r
                fib_levels.append(round(level, 4))
        info["fib_levels"] = fib_levels

        # 从 indicators 里尽量读布林带 / 枢轴 / VP（如果 calc_tech_indicators 没提供这些键，会是 None，不影响使用）
        info["bb_lower"] = indicators.get("bb_lower")
        info["bb_middle"] = indicators.get("bb_middle")
        info["bb_upper"] = indicators.get("bb_upper")
        info["pivot_levels"] = indicators.get("pivot_levels")
        info["vp_levels"] = indicators.get("vp_levels")

        self.signals[symbol] = info

    # ----- 输出主表 -----

    def print_summary(self):
        header = f"{'Symbol':<8} {'Signal':<6} {'P&L%':<8} {'Last':<10} {'Cost':<10} {'EMA20':<10} {'VOL20':<10} Action"
        print(header)
        print("-" * len(header))
        for symbol in self.watchlist:
            info = self.signals.get(symbol)
            if not info:
                print(f"{symbol:<8} {'?':<6} {'-':<8} {'-':<10} {'-':<10} {'-':<10} {'-':<10} 无数据")
                continue

            pnl_val = info.get("pnl_pct")
            pnl_str = "-" if pnl_val is None else f"{pnl_val:.2f}"

            cost_val = info.get("cost")
            cost_str = "-" if cost_val is None else f"{cost_val:.2f}"

            # 当前价优先使用 last_price，没有则退回 last_close
            last_val = info.get("last_price")
            if last_val is None:
                last_val = info.get("last_close")
            last_str = "-" if last_val is None else f"{float(last_val):.2f}"

            ema20_val = info.get("ema20")
            ema20_str = "-" if ema20_val is None else f"{ema20_val:.2f}"

            vol20_val = info.get("vol20")
            vol20_str = "-" if vol20_val is None else f"{vol20_val}"

            signal = info.get("signal", "?")

            print(
                f"{symbol:<8} "
                f"{signal:<6} "
                f"{pnl_str:<8} "
                f"{last_str:<10} "
                f"{cost_str:<10} "
                f"{ema20_str:<10} "
                f"{vol20_str:<10} "
                f"{info.get('action', '')}"
            )

    def save_csv_report(self, filename: str = "ib_strategy_report.csv"):
        """
        将当前所有可用持仓的信号 / 关键数据导出到 CSV 文件。
        """
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            out_path = os.path.join(script_dir, filename)

            fieldnames = [
                "symbol",
                "mode",
                "signal",
                "pnl_pct",
                "last",
                "cost",
                "ema20",
                "ma50",
                "ma200",
                "vol20",
                "vwap",
                "signal_15m",
                "signal_1h",
                "action",
            ]

            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()

                for symbol in self.watchlist:
                    info = self.signals.get(symbol)
                    if not info:
                        continue
                    if info.get("reason") == "No security definition" or info.get("signal") == "-":
                        continue

                    # CSV 中的 last 同样优先使用 last_price
                    last_val = info.get("last_price")
                    if last_val is None:
                        last_val = info.get("last_close")

                    row = {
                        "symbol": symbol,
                        "mode": self.mode,
                        "signal": info.get("signal"),
                        "pnl_pct": info.get("pnl_pct"),
                        "last": last_val,
                        "cost": info.get("cost"),
                        "ema20": info.get("ema20"),
                        "ma50": info.get("ma50"),
                        "ma200": info.get("ma200"),
                        "vol20": info.get("vol20"),
                        "vwap": info.get("vwap"),
                        "signal_15m": info.get("signal_15m"),
                        "signal_1h": info.get("signal_1h"),
                        "action": info.get("action"),
                    }
                    writer.writerow(row)

            print(f"📁 已导出 CSV 策略报告：{out_path}")
        except Exception as e:
            print(f"⚠ 导出 CSV 失败: {e}")

    # ----- 详细策略输出 -----

    def print_detailed_strategies(self):
        print("\n📊 各持仓股票详细交易策略")
        if self.mode == "intraday":
            print("   当前模式：盘中 intraday (15m + 1h + VWAP)")
        else:
            print("   当前模式：日线 daily (EMA / MACD / RSI / OBV)")
        print("   价格区间为参考，可结合盘感微调；仓位百分比按你当前持有量为 100% 计算。\n")

        # 先算一下面向股票部分的总市值，用来判断谁是重仓、谁是小仓
        total_pos_value = 0.0
        for symbol in self.watchlist:
            info = self.signals.get(symbol)
            if not info:
                continue
            pos_val = info.get("position_value")
            if isinstance(pos_val, (int, float)):
                total_pos_value += pos_val

        any_printed = False

        for symbol in self.watchlist:
            info = self.signals.get(symbol)
            if not info:
                continue
            if info.get("reason") == "No security definition" or info.get("signal") == "-":
                continue

            self._print_symbol_strategy(symbol, info, total_pos_value)
            any_printed = True

        if not any_printed:
            print("当前没有可以生成详细策略的股票（可能全部是无法获取历史数据的标的）。\n")

        print("📌 所有持仓股票关键价格带（基于成本价 + 最近区间高低点动态计算）:")
        for symbol in self.watchlist:
            info = self.signals.get(symbol)
            if not info:
                continue
            if info.get("reason") == "No security definition" or info.get("signal") == "-":
                continue
            self._print_brief_symbol_bands(symbol, info)

    def _print_brief_symbol_bands(self, symbol: str, info: Dict):
        cost = info.get("cost")
        bands_def = info.get("bands_defense")
        bands_off = info.get("bands_offense")
        if cost is None or not bands_def or not bands_off:
            return
        d1, d2, d3 = bands_def
        o1, o2, o3, o4 = bands_off
        print(
            f"  - {symbol}: 成本 {cost:.2f} | "
            f"防守价带 {d1}/{d2}/{d3} | "
            f"进攻&止盈价带 {o1}/{o2}/{o3}/{o4}"
        )

    def _print_symbol_strategy(self, symbol: str, info: Dict, total_pos_value: float):
        # 当前价：优先 last_price，没有则退回 last_close
        last = info.get("last_price")
        if last is None:
            last = info.get("last_close", 0.0)
        last = last or 0.0

        cost = info.get("cost")
        pnl = info.get("pnl_pct")
        signal = info.get("signal", "?")
        bands_def = info.get("bands_defense", [])
        bands_off = info.get("bands_offense", [])
        vwap = info.get("vwap")
        shares = info.get("shares")
        pos_value = info.get("position_value")
        pos_weight = None
        if isinstance(pos_value, (int, float)) and total_pos_value > 0:
            pos_weight = pos_value / total_pos_value

        # 防守价：bands_def 默认是从低到高，这里显式命名
        d_low, d_mid, d_high = (bands_def + [None, None, None])[:3]
        # 进攻 / 止盈价
        o1, o2, o3, o4 = (bands_off + [None, None, None, None])[:4]

        print("=" * 6, symbol, "交易策略", "=" * 6)
        print(
            f"当前价: {last:.3f}  | 成本: {cost if cost is not None else '-'}  | "
            f"浮盈/亏: {pnl if pnl is not None else '-'}%  | 综合信号: {signal}"
        )

        # === 模式分支：盘中 / 日线 ===
        if self.mode == "intraday":
            s15 = info.get("signal_15m")
            s1h = info.get("signal_1h")
            comment = info.get("intraday_comment", "")
            print(f"15m 信号: {s15}  | 1h 信号: {s1h}")
            if vwap is not None:
                print(f"当日 VWAP: {vwap}")
            print(f"盘中综合解读: {comment}\n")
        else:
            ema20 = info.get("ema20")
            ma50 = info.get("ma50")
            ma200 = info.get("ma200")
            trend = info.get("daily_trend", "")
            tech_comment = info.get("tech_comment", "")
            print(f"日线 EMA20: {ema20}  | MA50: {ma50}  | MA200: {ma200}")
            print(f"日线趋势判断: {trend}")
            print(f"多指标综合解读: {tech_comment}\n")

        # === 核心技术价位参考（daily + intraday 通用） ===
        core_levels = info.get("core_levels") or info.get("bands_core_levels")
        if core_levels:
            self._print_core_levels_block(core_levels)

        # 如果没有成本 / 关键价带，就到此为止
        if cost is None or not bands_def or not bands_off:
            print("（未获取到成本或关键价带，无法给出更细的阶梯策略）\n")
            return

        # 关键价格带 + 阶梯逻辑说明
        print(
            "[关键价格带]: "
            f"防守 {d_low} / {d_mid} / {d_high}，"
            f"进攻 & 止盈 {o1} / {o2} / {o3} / {o4}\n"
        )
        

        # === 核心技术价位（Fib / Bollinger / Pivot 等） ===
        core_pairs = info.get("core_levels") or []
        if core_pairs:
            # core_pairs 是一个 [(name, value), ...] 列表，我们先转成字典方便按名字取
            try:
                core = dict(core_pairs)
            except Exception:
                # 如果格式不对，就直接把原始内容打印出来，不影响主流程
                print("\n[核心技术价位参考]:")
                for item in core_pairs:
                    print(f"  · {item}")
            else:
                fib_382 = core.get("fib_382")
                fib_50  = core.get("fib_50")
                fib_618 = core.get("fib_618")

                bb_u = core.get("bb_upper")
                bb_m = core.get("bb_mid")
                bb_l = core.get("bb_lower")

                pivot = core.get("pivot")
                r1    = core.get("r1")
                r2    = core.get("r2")
                s1    = core.get("s1")
                s2    = core.get("s2")

                print("\n[核心技术价位参考]:")

                # 1）Fibonacci
                if fib_382 is not None and fib_50 is not None and fib_618 is not None:
                    print(
                        f"  Fibo 38.2% / 50% / 61.8%: "
                        f"{fib_382:.3f} / {fib_50:.3f} / {fib_618:.3f}"
                    )

                # 2）Bollinger Bands
                if bb_u is not None and bb_m is not None and bb_l is not None:
                    print(
                        f"  Bollinger 上 / 中 / 下轨: "
                        f"{bb_u:.3f} / {bb_m:.3f} / {bb_l:.3f}"
                    )

                # 3）Pivot Points
                if pivot is not None:
                    def _fmt(x):
                        return f"{x:.3f}" if x is not None else "-"

                    print(
                        "  Pivot / R1 / R2 / S1 / S2: "
                        f"{_fmt(pivot)} / {_fmt(r1)} / {_fmt(r2)} / {_fmt(s1)} / {_fmt(s2)}"
                    )

        # ===== 当前所处大致区间（先看整体跌幅，再从最深防守线向上判断） =====
        print("[当前所处大致区间]:")

        # bands_def 原始顺序是：成本*0.95, 0.90, 0.85
        # 这里按价格从低到高排一下：最深防守价 → 第二道 → 第一防守
        try:
            d_low, d_mid, d_high = sorted(bands_def)  # d_low 最深，d_high 最浅
        except Exception:
            d_low, d_mid, d_high = (None, None, None)

        # 计算整体浮盈/亏，用来识别“深度套牢区”
        drawdown_pct = None
        if cost is not None and cost > 0:
            drawdown_pct = (last / cost - 1.0) * 100.0

        # 用持仓市值占总市值判断是否“重仓 / 小仓”
        heavy_pos = pos_weight is not None and pos_weight >= 0.15   # ≥15% 视为重仓
        tiny_pos = pos_weight is not None and pos_weight <= 0.05    # ≤5% 视为小仓 / 冷冻仓

        if drawdown_pct is not None and drawdown_pct <= -25:
            # 深度套牢，按仓位大小区分处理
            if heavy_pos:
                zone = (
                    "重仓深度套牢（跌幅超过 25% 且该股占股票仓位约 "
                    f"{pos_weight*100:.1f}%），建议严肃评估是否执行紧急避险方案："
                    "如大幅减仓或阶段性清仓，优先保护账户整体安全。"
                )
            elif tiny_pos:
                zone = (
                    "小仓位深度套牢：对整体资金影响有限，可以视为冷冻仓位，"
                    "以等待反弹减仓或逐步退出为主，不必频繁操作。"
                )
            else:
                zone = (
                    "深度套牢（跌幅超过 25%），建议以降低风险为主："
                    "通过分批减仓 + 逢反弹主动卖出，逐步把持仓压缩到你可以接受的水平。"
                )
        elif d_low is not None and last < d_low:
            # 已跌破三道防守价，但跌幅有没有特别大，语气要区分
            if drawdown_pct is not None and drawdown_pct <= -15:
                zone = "跌破最深防守线（第三道支撑），建议明显收缩仓位，以风险控制优先。"
            else:
                zone = "跌破最深防守线，但整体回撤尚未超过 15%，以收缩仓位、降低风险为主，而不是一次性清仓。"
        elif d_mid is not None and last < d_mid:
            zone = "强防守区（第二道支撑被跌破），建议将仓位降到中等或偏低水平，优先考虑风险控制。"
        elif d_high is not None and last < d_high:
            zone = "轻度破位区（靠近第一道防守线），以防守为主，观察能否快速收回到防守带之上。"
        elif o1 is not None and last <= o1:
            zone = "靠近成本 + 轻微盈利区，一般是“重新评估趋势”的地带。"
        elif o2 is not None and last <= o2:
            zone = "首个压力带（约成本 +5% ~ +10%），适合小幅止盈或减轻仓位。"
        elif o3 is not None and last <= o3:
            zone = "主要止盈区（约成本 +10% ~ +15%），建议分批锁定较大部分利润。"
        elif o4 is not None and last <= o4:
            zone = "强化止盈区（约成本 +15% ~ +20%），进一步收割利润、降低风险。"
        else:
            zone = "远高于成本 +20%：情绪高位区，优先保护盈利，采用移动止损。"

        print(f"- {zone}\n")

        # ===== 向上进攻 / 止盈 =====
        print("[向上进攻 + 止盈阶梯]:")
        if o1 is not None:
            print(f"  · {o1} 附近：趋势健康时，可考虑减仓 10–20% 锁定一部分利润；不急于加仓。")
        if o2 is not None:
            print(f"  · 有效站上 {o2} 并放量：再减仓 20–30%，将仓位降到中等偏低水平。")
        if o3 is not None:
            print(f"  · 接近 {o3} 区间：作为主要止盈区，建议分批减掉 30–40% 的剩余仓位。")
        if o4 is not None:
            print(f"  · {o4} 及以上：视为高位区域，可将大部分仓位兑现，只保留 10–20% 让利润奔跑。\n")

        # ===== 向下防守 / 止损（从离成本最近的防守线开始说） =====
        print("[向下防守 / 止损策略]:")
        if d_high is not None:
            print(f"  · 收盘价跌破 {d_high} 且短线信号偏弱（D 为主）→ 建议减仓 20–30%。")
        if d_mid is not None:
            print(f"  · 再跌破 {d_mid} 且放量下跌 → 再减仓 30–40%，控制整体风险。")
        if d_low is not None:
            print(f"  · 跌到 {d_low} 以下且基本面没有明显改善 → 只保留少量仓位或考虑清仓。\n")

        # === 只读模式下：基于 ATR 价格带，打印一份“如果要下单”的挂单计划 ===
        _maybe_generate_order_plan_for_symbol(symbol, info)

    def _print_core_levels_block(self, core_levels):
        """
        根据 price_bands_engine 里写入的 core_levels，统一打印：
          - Fibo 38.2 / 50 / 61.8
          - Bollinger 上中下轨
          - Pivot + R1/R2/S1/S2

        core_levels 形如：[("fib_382", 32.126), ("fib_50", 31.655), ...]
        """
        try:
            if not core_levels:
                return

            kv = {}
            for k, v in core_levels:
                # 后写入的覆盖先写入的即可
                kv[str(k)] = float(v)

            fib_382 = kv.get("fib_382")
            fib_50  = kv.get("fib_50")
            fib_618 = kv.get("fib_618")

            bb_u = kv.get("bb_upper")
            bb_m = kv.get("bb_mid")
            bb_l = kv.get("bb_lower")

            pivot = kv.get("pivot")
            r1 = kv.get("r1")
            r2 = kv.get("r2")
            s1 = kv.get("s1")
            s2 = kv.get("s2")

            def fmt(x):
                return "-" if x is None else f"{x:.3f}"

            # 如果啥都没有，就不用打印
            if not any([fib_382, fib_50, fib_618, bb_u, bb_m, bb_l, pivot, r1, r2, s1, s2]):
                return

            print("[核心技术价位参考]:")
            if fib_382 is not None and fib_50 is not None and fib_618 is not None:
                print(
                    "  Fibo 38.2% / 50% / 61.8%: "
                    f"{fib_382:.3f} / {fib_50:.3f} / {fib_618:.3f}"
                )
            if bb_u is not None and bb_m is not None and bb_l is not None:
                print(
                    "  Bollinger 上 / 中 / 下轨: "
                    f"{bb_u:.3f} / {bb_m:.3f} / {bb_l:.3f}"
                )

            if pivot is not None:
                print(
                    "  Pivot / R1 / R2 / S1 / S2: "
                    f"{fmt(pivot)} / {fmt(r1)} / {fmt(r2)} / {fmt(s1)} / {fmt(s2)}"
                )

            print()  # 收尾空行，和你现在 daily 输出的风格保持一致

        except Exception as e:
            # 即使技术价位出问题，也绝不能影响主策略输出
            print(f"[核心技术价位参考生成出错: {e}]\n")

    # ----- 断开连接 -----

    def _disconnect_safely(self):
        print("\n🔌 正在断开连接 ...")
        self.disconnect()
        print("脚本结束。")


def _maybe_generate_order_plan_for_symbol(symbol: str, info: Dict) -> None:
    """
    为某只股票打印一份“如果要下单，会怎么挂单”的计划。
    这一版开始真正把 Fib / BB / Pivot / Volume Profile 等技术位，通过
    TechnicalLevels → PriceBandsContext → generate_price_bands 接进去。
    """
    # 当前价：优先 last_price，没有就退回 last_close
    last = info.get("last_price")
    if last is None:
        last = info.get("last_close")
    if not last or last <= 0:
        return

    # 只对已有持仓的标的生成计划（后续如果要对自选股生成，再单独扩展）
    cost = info.get("cost")
    if cost is None:
        return

    # 浮盈/亏 %
    pnl_pct = info.get("pnl_pct")

    # 估算这只股票占总资金的仓位比例（用 position_value / ACCOUNT_EQUITY_MANUAL）
    position_size_pct = None
    pos_value = info.get("position_value")
    if isinstance(pos_value, (int, float)) and ACCOUNT_EQUITY_MANUAL > 0:
        position_size_pct = round(pos_value / ACCOUNT_EQUITY_MANUAL * 100.0, 2)

    pos = PositionInfo(
        is_position=True,
        cost=cost,
        pnl_pct=pnl_pct,
        position_size_pct=position_size_pct,
    )

    # ===== 把日线分析阶段算好的技术位装进 TechnicalLevels =====
    fib_levels = info.get("fib_levels") or []          # 日线用最近 80 根高低点推的 Fib
    pivot_levels = info.get("pivot_levels") or []      # 日 Pivot（如果 indicator_rules 提供）
    vp_levels = (
        info.get("vp_levels")                          # Volume Profile 关键价位（如有）
        or info.get("volume_profile_levels")
        or []
    )

    tech = TechnicalLevels(
        fib_levels=fib_levels,
        bb_upper=info.get("bb_upper"),
        bb_middle=info.get("bb_middle"),
        bb_lower=info.get("bb_lower"),
        pivot_levels=pivot_levels,
        volume_profile_levels=vp_levels,
        manual_supports=[],        # 手工画的支撑/压力以后可以再接
        manual_resistances=[],
    )

    # ===== ATR：如果 info 里已有就用，没有则用“当前价的 3%”做一个简易估算 =====
    atr = info.get("atr")
    if not atr or atr <= 0:
        atr = max(float(last) * 0.03, 0.1)

    # === 根据日线分析结果推断一个模式标签 ===
    mode = _infer_mode_from_daily_info(info)

    # 方向：目前先统一用 up（做多），后面如果做空再扩展
    ctx = PriceBandsContext(
        symbol=symbol,
        last_price=float(last),
        atr=float(atr),
        mode=mode,
        trend_direction="up",
        tech=tech,
        pos=pos,
    )

    # 调用价格带引擎（内部会先做风控过滤，再算 ATR+技术共振的价带）
    bands = generate_price_bands(ctx)

    # 如果有 core_levels（Fibo / Bollinger / Pivot 等核心价位），
    # 也同步放到 info 里，方便后面打印“各持仓股票详细交易策略”时使用。
    core_levels = getattr(bands, "core_levels", None)
    if core_levels:
        info["core_levels"] = core_levels

    # side='NONE'：要么是风控过滤（深度套牢 / 冷冻仓位），要么是模式未实现
    # 这种情况就不再生成自动挂单计划
    if bands.side == "NONE":
        return

    # 用账户资金 + 单笔风险比例，生成一个 OrderPlan（只打印，不下单）
    plan = build_order_plan_from_bands(
        bands,
        ACCOUNT_EQUITY_MANUAL,
        RISK_PER_TRADE_PCT,
    )

    print_order_plan(plan)


def main():
    host = "127.0.0.1"
    port = 4001
    client_id = 7

    mode = "intraday" if is_us_market_open_now() else "daily"

    app = Intraday15mStrategyAutoPosApp(host=host, port=port, client_id=client_id, mode=mode)

    print(f"尝试连接 IB Gateway {host}:{port}, clientId={client_id} ...")
    app.connect(host, port, client_id)

    t = threading.Thread(target=app.run, daemon=True)
    t.start()

    try:
        while app.isConnected():
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("收到手动中断信号，准备断开连接 ...")
        app._disconnect_safely()


if __name__ == "__main__":
    main()