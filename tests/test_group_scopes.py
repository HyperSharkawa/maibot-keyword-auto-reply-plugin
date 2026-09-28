"""隔离验证群范围契约，使用真实框架配置方法，替换注册、日志和发送接口以避免启动机器人。"""

import ast
import copy
import json
import os
import re
import runpy
import sys
import tomllib
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, mock_open, patch


ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK_ROOT = Path(os.environ.get("MAIBOT_ROOT", str(ROOT.parent / "MaiBot")))


class IsolatedPluginBase:
    """承载插件配置的测试基类，不加载真实 manifest、注册表或后台任务。"""


class IsolatedEventHandler:
    """提供事件元信息契约，消息发送和配置读取由各测试显式模拟。"""

    intercept_message = False

    @classmethod
    def get_handler_info(cls):
        """返回待测处理器的名称及拦截标记，不向框架注册组件。"""
        return SimpleNamespace(name=cls.handler_name, intercept_message=cls.intercept_message)


def _register_plugin(plugin_class):
    """直接返回待测插件类，避免导入时修改框架注册表。"""
    return plugin_class


def _load_plugin():
    """读取指定框架的配置类型并隔离导入插件，返回模块命名空间，不加载框架业务模块。"""
    fields = runpy.run_path(str(FRAMEWORK_ROOT / "src/plugin_system/base/config_types.py"))
    api = ModuleType("src.plugin_system")
    api.__dict__.update(
        BasePlugin=IsolatedPluginBase,
        BaseEventHandler=IsolatedEventHandler,
        ConfigField=fields["ConfigField"],
        EventType=SimpleNamespace(ON_MESSAGE="on_message"),
        EventHandlerInfo=SimpleNamespace,
        MaiMessages=SimpleNamespace,
        register_plugin=_register_plugin,
    )
    logger_module = ModuleType("src.common.logger")
    logger_module.get_logger = Mock(return_value=Mock())
    modules = {
        "src": ModuleType("src"),
        "src.common": ModuleType("src.common"),
        "src.common.logger": logger_module,
        "src.plugin_system": api,
    }
    module = ModuleType("keyword_auto_reply_under_test")
    path = ROOT / "plugin.py"
    module.__file__ = str(path)
    modules[module.__name__] = module
    with patch.dict(sys.modules, modules):
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


plugin = _load_plugin()


def _attach_framework_config_methods():
    """将框架原始配置方法附到测试基类，验证实际序列化行为而不导入框架启动依赖。"""
    path = FRAMEWORK_ROOT / "src/plugin_system/base/plugin_base.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    base = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PluginBase")
    names = {
        "_format_toml_value",
        "_generate_and_save_default_config",
        "_save_config_to_file",
        "_get_expected_config_version",
        "get_manifest_info",
    }
    methods = [node for node in base.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"Any": Any, "Dict": Dict, "json": json, "ConfigField": plugin.ConfigField, "logger": Mock()}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), "exec"), namespace)
    for name in names:
        setattr(IsolatedPluginBase, name, namespace[name])


_attach_framework_config_methods()


def make_rule(**changes):
    """返回独立的合法规则，changes 用于构造范围、发送类型和非法输入场景。"""
    return {
        "enabled": "启用",
        "group_scope": "继承默认",
        "groups": "",
        "reply_type": "文本",
        "pattern": "hello",
        "reply_content": "你好。",
        "description": "测试规则",
        **changes,
    }


def make_plugin(default_groups, rules):
    """返回持有指定默认群名单及规则的隔离插件实例，不生成文件或注册组件。"""
    instance = plugin.KeywordAutoReplyPlugin()
    instance.config = {"keyword_auto_reply": {"enable": True, "default_groups": default_groups, "rules": rules}}
    return instance


class GroupScopeConfigTests(unittest.TestCase):
    """验证加载契约、失败时缓存完整性及真实框架配置文件读写。"""

    def setUp(self):
        """为每个测试隔离共享规则缓存，结束后恢复原状态。"""
        self.cache_patch = patch.object(plugin.KeywordAutoReplyHandler, "rules_cache", plugin.RuleCache((), frozenset()))
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def test_scope_resolution_and_inactive_text(self):
        """校验继承、覆盖及无限制标记，同时确认非指定模式不解析保留文本。"""
        rules = [
            make_rule(groups="保留文本，尚未填写"),
            make_rule(group_scope="指定群聊", groups="333,444"),
            make_rule(group_scope="所有群聊", groups="保留文本"),
            make_rule(group_scope="指定群聊"),
        ]
        instance = make_plugin(["111", "222"], rules)
        original = copy.deepcopy(instance.config)
        instance.get_plugin_components()
        self.assertEqual(
            [rule["allowed_groups"] for rule in plugin.KeywordAutoReplyHandler.rules_cache.rules],
            [frozenset({"111", "222"}), frozenset({"333", "444"}), None, frozenset()],
        )
        self.assertEqual(instance.config, original)

    def test_default_group_contract(self):
        """默认名单只接受数字字符串列表，空名单有效，错误明确定位配置项。"""
        invalid = ["111", None, 111, [111], [True], [""], [" 111"], ["111 "], ["１２３"], ["1a"], ["1\n"]]
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "keyword_auto_reply.default_groups"):
                make_plugin(value, []).get_plugin_components()
        make_plugin([], [make_rule()]).get_plugin_components()
        self.assertEqual(plugin.KeywordAutoReplyHandler.rules_cache.rules[0]["allowed_groups"], frozenset())

    def test_missing_fields_reject_old_configuration(self):
        """旧全局键和缺少范围字段的旧规则必须报错，不能默认继承或放行。"""
        instance = make_plugin([], [])
        instance.config["keyword_auto_reply"] = {"enabled_groups": ["111"], "rules": [make_rule()]}
        with self.assertRaisesRegex(ValueError, "keyword_auto_reply.default_groups"):
            instance.get_plugin_components()
        for field in ("group_scope", "groups"):
            rule = make_rule()
            del rule[field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, re.escape(f"rules[0].{field}")):
                make_plugin([], [rule]).get_plugin_components()

    def test_rule_field_types_and_scope_values(self):
        """范围必须是合法模式，群号文本即使暂不生效也必须保持字符串类型。"""
        for scope in (None, True, 1, [], "", "默认", "继承默认 "):
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, r"rules\[0\]\.group_scope"):
                make_plugin([], [make_rule(group_scope=scope)]).get_plugin_components()
        # 所有模式都要求保留字段的字符串类型，即使暂时不会解析名单内容。
        for scope in plugin.GROUP_SCOPES:
            for groups in (None, 111, [], ["111"]):
                with self.subTest(scope=scope, groups=groups), self.assertRaisesRegex(ValueError, r"rules\[0\]\.groups"):
                    make_plugin([], [make_rule(group_scope=scope, groups=groups)]).get_plugin_components()

    def test_custom_groups_reject_invalid_separators_and_characters(self):
        """指定名单不能通过清洗空白、空项或非 ASCII 数字来接受不符合契约的输入。"""
        invalid = [" ", "111，222", "111,,222", "111,", ",111", "111, 222", "１２３", "-1", "1a", "111\n"]
        for groups in invalid:
            with self.subTest(groups=groups), self.assertRaisesRegex(ValueError, r"rules\[0\]\.groups"):
                make_plugin([], [make_rule(group_scope="指定群聊", groups=groups)]).get_plugin_components()

    def test_disabled_rules_are_validated_before_filtering(self):
        """停用规则同样暴露缺字段和非法名单，通过校验后才从匹配缓存排除。"""
        invalid_rules = [
            make_rule(enabled="不启用", group_scope="错误模式"),
            make_rule(enabled="不启用", group_scope="指定群聊", groups="111,"),
        ]
        missing = make_rule(enabled="不启用")
        del missing["groups"]
        invalid_rules.append(missing)
        for rule in invalid_rules:
            with self.subTest(rule=rule), self.assertRaisesRegex(ValueError, r"rules\[1\]"):
                make_plugin([], [make_rule(), rule]).get_plugin_components()
        make_plugin([], [make_rule(enabled="不启用")]).get_plugin_components()
        self.assertEqual(plugin.KeywordAutoReplyHandler.rules_cache, plugin.RuleCache((), frozenset()))

    def test_invalid_containers_report_paths(self):
        """错误配置节、规则列表及规则对象均在加载阶段明确失败。"""
        instance = make_plugin([], [])
        instance.config = {}
        with self.assertRaisesRegex(ValueError, "keyword_auto_reply"):
            instance.get_plugin_components()
        for rules in (None, "rules", {}):
            with self.subTest(rules=rules), self.assertRaisesRegex(ValueError, "keyword_auto_reply.rules"):
                make_plugin([], rules).get_plugin_components()
        with self.assertRaisesRegex(ValueError, r"rules\[0\]"):
            make_plugin([], [None]).get_plugin_components()

    def test_failed_reload_does_not_publish_partial_cache(self):
        """后续规则校验失败时保留旧缓存，避免新名单被部分应用。"""
        make_plugin(["111"], [make_rule()]).get_plugin_components()
        previous = plugin.KeywordAutoReplyHandler.rules_cache
        with self.assertRaisesRegex(ValueError, r"rules\[1\]\.group_scope"):
            make_plugin(["222"], [make_rule(), make_rule(group_scope="错误模式")]).get_plugin_components()
        self.assertIs(plugin.KeywordAutoReplyHandler.rules_cache, previous)
        self.assertEqual(previous.rules[0]["allowed_groups"], frozenset({"111"}))
        self.assertEqual(previous.covered_groups, frozenset({"111"}))

    def test_schema_and_framework_toml_round_trip(self):
        """用框架原始生成与保存方法验证新默认值、字段类型及中文配置能完整读回。"""
        schema = plugin.KeywordAutoReplyPlugin.config_schema["keyword_auto_reply"]
        self.assertNotIn("enabled_groups", schema)
        self.assertEqual(schema["default_groups"].default, [])
        fields = schema["rules"].to_dict()["item_fields"]
        self.assertEqual(fields["group_scope"]["choices"], ["继承默认", "指定群聊", "所有群聊"])
        self.assertEqual(fields["group_scope"]["default"], "继承默认")
        self.assertEqual(fields["groups"]["default"], "")
        instance = make_plugin([], [])
        instance.manifest_data = {}
        instance.log_prefix = "测试插件"
        file_open = mock_open()
        # 仅模拟文件句柄，仍执行框架原始生成与保存逻辑，避免测试依赖临时目录权限。
        with patch.dict(instance._generate_and_save_default_config.__globals__, open=file_open):
            instance._generate_and_save_default_config("config.toml")
            generated = tomllib.loads(file_open().write.call_args.args[0])
            instance.config = generated
            instance.get_plugin_components()
            self.assertEqual(plugin.KeywordAutoReplyHandler.rules_cache.rules[0]["allowed_groups"], frozenset())
            self.assertEqual(generated["keyword_auto_reply"]["rules"][0]["group_scope"], "继承默认")
            self.assertEqual(generated["keyword_auto_reply"]["rules"][0]["groups"], "")
            generated["keyword_auto_reply"]["default_groups"] = ["111"]
            generated["keyword_auto_reply"]["rules"].append(make_rule(group_scope="指定群聊", groups="222,333"))
            instance._save_config_to_file(generated, "config.toml")
            self.assertEqual(tomllib.loads(file_open().write.call_args.args[0]), generated)

    def test_readme_configuration_is_loadable(self):
        """文档中的完整 TOML 示例必须通过真实规则加载，避免说明与实现脱节。"""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        example = readme.split("```toml\n", 1)[1].split("```", 1)[0]
        instance = make_plugin([], [])
        instance.config = tomllib.loads(example)
        instance.get_plugin_components()
        self.assertEqual(len(plugin.KeywordAutoReplyHandler.rules_cache.rules), 3)


class GroupScopeMessageTests(unittest.IsolatedAsyncioTestCase):
    """通过模拟发送验证群范围、匹配顺序及原有发送结果语义。"""

    def setUp(self):
        """隔离类缓存并配置发送模拟；所有测试均不访问真实消息接口。"""
        self.cache_patch = patch.object(plugin.KeywordAutoReplyHandler, "rules_cache", plugin.RuleCache((), frozenset()))
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.handler = plugin.KeywordAutoReplyHandler()
        self.handler.get_config = Mock(return_value=True)
        self.handler.send_text = AsyncMock(return_value=True)
        self.handler.send_image = AsyncMock(return_value=True)
        self.message = SimpleNamespace(
            is_group_message=True, stream_id="test-stream", message_base_info={"group_id": "111"}, plain_text="hello"
        )

    async def test_scope_matrix(self):
        """覆盖三种模式与空名单组合，确认仅所有群聊模式无条件允许群消息。"""
        cases = [
            (["111"], "继承默认", "", True),
            (["222"], "继承默认", "", False),
            ([], "继承默认", "", False),
            (["222"], "指定群聊", "111", True),
            ([], "指定群聊", "111", True),
            (["111"], "指定群聊", "222", False),
            (["111"], "指定群聊", "", False),
            ([], "所有群聊", "", True),
            (["222"], "所有群聊", "", True),
        ]
        # 每个用例重新加载并清空发送记录，保证验证的是本次范围决策。
        for defaults, scope, groups, expected in cases:
            with self.subTest(defaults=defaults, scope=scope, groups=groups):
                self.handler.send_text.reset_mock()
                make_plugin(defaults, [make_rule(group_scope=scope, groups=groups)]).get_plugin_components()
                result = await self.handler.execute(self.message)
                self.assertEqual(self.handler.send_text.await_count, int(expected))
                self.assertTrue(result[1])

    async def test_private_and_disabled_messages_do_not_send(self):
        """所有群聊仍不处理私聊，插件总开关仍可关闭回复。"""
        make_plugin([], [make_rule(group_scope="所有群聊")]).get_plugin_components()
        self.message.is_group_message = False
        await self.handler.execute(self.message)
        self.message.is_group_message = True
        self.handler.get_config.return_value = False
        await self.handler.execute(self.message)
        self.handler.send_text.assert_not_awaited()
        self.handler.send_image.assert_not_awaited()

    async def test_disallowed_rule_skips_regex_and_allows_later_rule(self):
        """前一规则不允许当前群时不执行它的正则，后续相同正则规则仍可回复。"""
        rules = [
            make_rule(group_scope="指定群聊", groups="222", reply_content="不应发送"),
            make_rule(group_scope="指定群聊", groups="111", reply_content="后续规则"),
        ]
        make_plugin([], rules).get_plugin_components()
        cached_rules = plugin.KeywordAutoReplyHandler.rules_cache.rules
        denied = Mock()
        denied.search.side_effect = AssertionError("不允许当前群时不应匹配正则")
        allowed = Mock(wraps=cached_rules[1]["compiled_pattern"])
        cached_rules[0]["compiled_pattern"] = denied
        cached_rules[1]["compiled_pattern"] = allowed
        await self.handler.execute(self.message)
        denied.search.assert_not_called()
        allowed.search.assert_called_once_with("hello")
        self.handler.send_text.assert_awaited_once_with(stream_id="test-stream", text="后续规则", storage_message=True)

    async def test_image_scope_and_first_success(self):
        """图片规则使用同一范围判断；拒绝时不读文件，成功时不再发送后续文本。"""
        image_rule = make_rule(group_scope="指定群聊", groups="222", reply_type="图片", reply_content="test.png")
        with patch.object(plugin, "_load_image_as_base64", return_value="图片编码") as image_loader:
            make_plugin([], [image_rule]).get_plugin_components()
            await self.handler.execute(self.message)
            image_loader.assert_not_called()
            self.handler.send_image.assert_not_awaited()
            image_rule["groups"] = "111"
            make_plugin([], [image_rule, make_rule(group_scope="所有群聊")]).get_plugin_components()
            await self.handler.execute(self.message)
            image_loader.assert_called_once_with("test.png")
        self.handler.send_image.assert_awaited_once_with(
            stream_id="test-stream", image_base64="图片编码", storage_message=True
        )
        self.handler.send_text.assert_not_awaited()

    async def test_first_text_success_stops_matching(self):
        """多条可回复规则保持按顺序仅发送首条成功文本，并继续框架后续处理。"""
        make_plugin(["111"], [make_rule(reply_content="首条"), make_rule(reply_content="次条")]).get_plugin_components()
        later_pattern = Mock()
        later_pattern.search.side_effect = AssertionError("成功回复后不应再匹配后续正则")
        plugin.KeywordAutoReplyHandler.rules_cache.rules[1]["compiled_pattern"] = later_pattern
        result = await self.handler.execute(self.message)
        self.handler.send_text.assert_awaited_once_with(stream_id="test-stream", text="首条", storage_message=True)
        later_pattern.search.assert_not_called()
        self.assertTrue(result[0])
        self.assertTrue(result[1])
        self.assertFalse(self.handler.intercept_message)

    async def test_send_failure_still_stops_matching(self):
        """文本或图片发送返回失败时都结束匹配，不尝试后续规则。"""
        for reply_type in ("文本", "图片"):
            with self.subTest(reply_type=reply_type):
                self.handler.send_text.reset_mock()
                self.handler.send_image.reset_mock()
                self.handler.send_text.return_value = False
                self.handler.send_image.return_value = False
                make_plugin(["111"], [make_rule(reply_type=reply_type), make_rule()]).get_plugin_components()
                with patch.object(plugin, "_load_image_as_base64", return_value="图片编码"):
                    result = await self.handler.execute(self.message)
                self.assertFalse(result[0])
                self.assertTrue(result[1])
                self.assertEqual(self.handler.send_text.await_count + self.handler.send_image.await_count, 1)

    async def test_bad_regex_and_unreadable_image_allow_later_rule(self):
        """保留非法正则及图片读取失败跳过当前规则的行为。"""
        make_plugin(["111"], [make_rule(pattern="["), make_rule(reply_type="图片"), make_rule()]).get_plugin_components()
        with patch.object(plugin, "_load_image_as_base64", side_effect=FileNotFoundError):
            await self.handler.execute(self.message)
        self.handler.send_image.assert_not_awaited()
        self.handler.send_text.assert_awaited_once()

    async def test_config_changes_require_registration_and_replace_cache(self):
        """配置内名单修改不会影响旧快照，重新注册后继承范围与自定义范围均更新。"""
        instance = make_plugin(["111"], [make_rule(), make_rule(group_scope="指定群聊", groups="111")])
        instance.get_plugin_components()
        instance.config["keyword_auto_reply"]["default_groups"][:] = ["222"]
        instance.config["keyword_auto_reply"]["rules"][1]["groups"] = "222"
        await self.handler.execute(self.message)
        self.handler.send_text.assert_awaited_once()
        self.handler.send_text.reset_mock()
        instance.get_plugin_components()
        await self.handler.execute(self.message)
        self.handler.send_text.assert_not_awaited()
        self.message.message_base_info["group_id"] = "222"
        await self.handler.execute(self.message)
        self.handler.send_text.assert_awaited_once()
        instance.config["keyword_auto_reply"]["rules"] = []
        instance.get_plugin_components()
        self.assertEqual(plugin.KeywordAutoReplyHandler.rules_cache, plugin.RuleCache((), frozenset()))

    async def test_uncovered_groups_return_before_rule_iteration_and_text_processing(self):
        """无候选群和全部空名单都须直接返回，不遍历规则或访问文本处理方法。"""
        # 不可迭代对象与无文本方法的对象用于证明返回发生在规则循环和文本清洗之前。
        self.message.plain_text = object()
        for covered in (frozenset(), frozenset({"222"})):
            with self.subTest(covered=covered):
                plugin.KeywordAutoReplyHandler.rules_cache = plugin.RuleCache(object(), covered)
                result = await self.handler.execute(self.message)
                self.assertIn("没有可触发的规则", result[2])
        self.handler.send_text.assert_not_awaited()

    async def test_messages_reuse_compiled_patterns(self):
        """加载后匹配直接复用正则对象，不再调用字符串正则入口或重新编译。"""
        make_plugin(["111"], [make_rule()]).get_plugin_components()
        with (
            patch.object(plugin.re, "compile", side_effect=AssertionError("消息处理不应重新编译")),
            patch.object(plugin.re, "search", side_effect=AssertionError("消息处理不应查询字符串正则缓存")),
        ):
            await self.handler.execute(self.message)
            await self.handler.execute(self.message)
        self.assertEqual(self.handler.send_text.await_count, 2)


if __name__ == "__main__":
    unittest.main()
