"""输出路由 —— 从 deepseek-agent-desktop 的 TerminalOutputRouter 移植。

三档策略（数值全部沿用原版实战取值）：
  <= 16,000 字符      直出
  >  16,000 字符      7,500 头 + 7,500 尾（中段信息量最低）
  >  5 MB             预闸拒绝（不进入回传通道）
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ExecutorConfig


@dataclass
class RoutedOutput:
    text: str
    tier: str
    original_chars: int
    delivered_chars: int
    note: str = ""

    @property
    def truncated(self) -> bool:
        return self.tier != "direct"


class OutputRouter:
    def __init__(self, cfg: ExecutorConfig) -> None:
        self.cfg = cfg

    def route(self, text: str) -> RoutedOutput:
        original = len(text)
        hard = self.cfg.hard_output_limit

        if len(text.encode("utf-8", errors="replace")) > hard:
            head = text[: self.cfg.head_tail_chunk]
            tail = text[-self.cfg.head_tail_chunk :]
            body = (
                f"{head}\n\n"
                f"[输出超过 {hard // (1024 * 1024)} MB 预闸上限，已拒绝完整回传；"
                f"原始 {original} 字符]\n\n{tail}"
            )
            return RoutedOutput(
                text=body,
                tier="rejected",
                original_chars=original,
                delivered_chars=len(body),
                note=f"超出 {hard // (1024 * 1024)} MB 预闸",
            )

        if original <= self.cfg.direct_output_limit:
            return RoutedOutput(
                text=text,
                tier="direct",
                original_chars=original,
                delivered_chars=original,
            )

        chunk = self.cfg.head_tail_chunk
        omitted = original - chunk * 2
        body = (
            f"{text[:chunk]}\n\n"
            f"[中段已省略 {omitted} 字符 —— 完整输出见执行监视器]\n\n"
            f"{text[-chunk:]}"
        )
        return RoutedOutput(
            text=body,
            tier="head_tail",
            original_chars=original,
            delivered_chars=len(body),
            note=f"省略中段 {omitted} 字符",
        )
