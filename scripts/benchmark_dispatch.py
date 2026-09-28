"""测量隔离消息处理耗时，不加载机器人、不发送消息；结果不包含框架事件分发和网络开销。"""

import asyncio
import logging
import runpy
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SUPPORT = runpy.run_path(str(ROOT / "tests/test_group_scopes.py"))
plugin = SUPPORT["plugin"]
make_plugin = SUPPORT["make_plugin"]
make_rule = SUPPORT["make_rule"]


def get_enabled(key, default):
    """模拟总开关读取，返回启用状态，避免模拟调用记录污染每条消息的计时。"""
    return True


async def measure(handler, message, iterations):
    """对指定消息先预热，再取五轮平均耗时的中位数，返回每条消息的微秒数。"""
    for _ in range(10):
        await handler.execute(message)
    samples = []
    # 每轮只计处理器执行，不计配置生成；固定消息保证前后版本的输入一致。
    for _ in range(5):
        start = perf_counter_ns()
        for _ in range(iterations):
            await handler.execute(message)
        samples.append((perf_counter_ns() - start) / iterations / 1000)
    return median(samples)


async def main():
    """输出不同规则数量和群范围下的本机合成负载结果，仅修改隔离插件的内存缓存。"""
    logger = logging.getLogger("keyword_reply_benchmark")
    logger.disabled = True
    plugin.logger = logger
    handler = plugin.KeywordAutoReplyHandler()
    handler.get_config = get_enabled
    message = SimpleNamespace(
        is_group_message=True,
        stream_id="benchmark-stream",
        message_base_info={"group_id": "111"},
        plain_text="一条不会命中任何测试规则的普通群消息",
    )
    print("规则数\t无覆盖群（微秒/条）\t空名单（微秒/条）\t可匹配群未命中（微秒/条）")
    # 规则正则彼此不同，以覆盖规则数超过正则缓存容量的情况；消息始终不命中，不执行发送。
    for count in (10, 100, 1000):
        rules = [make_rule(pattern=f"^keyword_{index}$") for index in range(count)]
        make_plugin(["222"], rules).get_plugin_components()
        unrelated = await measure(handler, message, 2000)
        make_plugin([], rules).get_plugin_components()
        empty = await measure(handler, message, 2000)
        make_plugin(["111"], rules).get_plugin_components()
        allowed = await measure(handler, message, 100)
        print(f"{count}\t{unrelated:.3f}\t{empty:.3f}\t{allowed:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
