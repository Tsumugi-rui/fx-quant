"""手续费结构诊断。

手续费 = margin * leverage * (0.00005 + leverage * 0.000005)

对杠杆是二次增长，因此高杠杆的成本侵蚀极快。
本脚本量化不同杠杆下的「手续费占保证金比例」与「打平所需价格波动」，
用于确定合理的杠杆上限。这是纯成本分析，不涉及价格预测。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fxquant.config import trade_fee


def main() -> None:
    margin = 500.0
    print(f"固定保证金 ${margin:.0f}，观察手续费随杠杆的变化")
    print(f"{'杠杆':>5}{'名义仓位':>12}{'手续费':>10}{'费/保证金':>11}"
          f"{'打平需波动':>12}")
    print("-" * 52)
    for lev in (1, 3, 5, 10, 20, 30, 50, 75, 100):
        notional = margin * lev
        fee = trade_fee(margin, lev)
        fee_ratio = fee / margin
        # 打平所需价格波动 = 手续费 / 名义仓位
        breakeven = fee / notional
        print(f"{lev:>5}{notional:>12,.0f}{fee:>10.2f}{fee_ratio:>11.2%}"
              f"{breakeven:>12.4%}")

    print()
    print("结论：杠杆越高，手续费占保证金比例越高，")
    print("      且打平所需的价格波动也随杠杆线性放大。")


if __name__ == "__main__":
    main()
