import base64
import os
import re
from typing import Any, List, NamedTuple, Optional, Tuple, TypedDict

from src.common.logger import get_logger
from src.plugin_system import BaseEventHandler, BasePlugin, ConfigField, EventType, EventHandlerInfo, MaiMessages, register_plugin

logger = get_logger("keyword_auto_reply_plugin")

# 回复类型枚举：仅允许文本或图片，写错会在规则清洗阶段被剔除
REPLY_TYPE_TEXT = "文本"
REPLY_TYPE_IMAGE = "图片"
ALLOWED_REPLY_TYPES = {REPLY_TYPE_TEXT, REPLY_TYPE_IMAGE}
GROUP_SCOPE_DEFAULT = "继承默认"
GROUP_SCOPE_CUSTOM = "指定群聊"
GROUP_SCOPE_ALL = "所有群聊"
GROUP_SCOPES = (GROUP_SCOPE_DEFAULT, GROUP_SCOPE_CUSTOM, GROUP_SCOPE_ALL)


class ReplyRule(TypedDict):
	"""加载后的规则；群集合为空表示拒绝全部，None 表示所有群；编译结果为字符串时记录正则错误。"""

	pattern: str
	compiled_pattern: re.Pattern[str] | str
	reply_type: str
	reply_content: str
	description: str
	allowed_groups: frozenset[str] | None


class RuleCache(NamedTuple):
	"""一次发布的规则快照；覆盖群集合用于排除无候选群，None 表示存在所有群聊规则。"""

	rules: tuple[ReplyRule, ...]
	covered_groups: frozenset[str] | None


def _build_rule_cache(rules: list[ReplyRule]) -> RuleCache:
	"""从已校验规则计算覆盖群集合并返回快照，不修改规则或共享缓存。"""
	covered_groups: set[str] = set()
	# 任意规则允许所有群时不能整体排除任何群；否则合并所有规则范围供常数时间查询。
	for rule in rules:
		allowed_groups = rule["allowed_groups"]
		if allowed_groups is None:
			return RuleCache(tuple(rules), None)
		covered_groups.update(allowed_groups)
	return RuleCache(tuple(rules), frozenset(covered_groups))


def _parse_group_ids(raw_groups: Any, field_path: str) -> frozenset[str]:
	"""校验群号列表并返回只读集合；field_path 用于定位错误，不转换或修改输入。"""
	if not isinstance(raw_groups, list):
		raise ValueError(f"{field_path} 必须是群号字符串列表")

	# 群号只接受约定的数字字符串，发现非法项立即指出位置，避免名单被意外扩大。
	for index, group_id in enumerate(raw_groups):
		if not isinstance(group_id, str) or re.fullmatch(r"[0-9]+", group_id) is None:
			raise ValueError(f"{field_path}[{index}] 必须是非空 ASCII 数字字符串，不允许空白或其他字符")
	return frozenset(raw_groups)


def _normalize_rules(raw_rules: Any, default_groups: frozenset[str]) -> list[ReplyRule]:
	"""校验全部规则的群范围，返回包含有效群集合的启用规则，不修改输入或缓存。

	default_groups 仅供继承模式使用；新字段缺失或无效时抛出带规则下标的错误。
	其余回复字段保留现有清洗行为，reply_content 表示文本内容或本地图片路径。
	"""
	if not isinstance(raw_rules, list):
		raise ValueError("keyword_auto_reply.rules 必须是规则对象列表")

	normalized_rules: list[ReplyRule] = []
	# 每条规则先校验群范围，再筛选启用状态，防止停用规则隐藏旧配置或无效字段。
	for index, raw_rule in enumerate(raw_rules):
		rule_path = f"keyword_auto_reply.rules[{index}]"
		if not isinstance(raw_rule, dict):
			raise ValueError(f"{rule_path} 必须是规则对象")
		# 两个字段均为新契约的必填项，不能给缺字段的旧规则静默补默认值。
		for field in ("group_scope", "groups"):
			if field not in raw_rule:
				raise ValueError(f"{rule_path}.{field} 缺少必填字段")

		group_scope = raw_rule["group_scope"]
		if not isinstance(group_scope, str) or group_scope not in GROUP_SCOPES:
			raise ValueError(f"{rule_path}.group_scope 必须是以下字符串之一：{'、'.join(GROUP_SCOPES)}")
		groups = raw_rule["groups"]
		if not isinstance(groups, str):
			raise ValueError(f"{rule_path}.groups 必须是英文逗号分隔的群号字符串，允许空字符串")

		allowed_groups: frozenset[str] | None
		# 覆盖模式不与默认名单取交集；只有明确选择所有群聊才使用无限制标记。
		if group_scope == GROUP_SCOPE_DEFAULT:
			allowed_groups = default_groups
		elif group_scope == GROUP_SCOPE_CUSTOM:
			allowed_groups = _parse_group_ids(groups.split(",") if groups else [], f"{rule_path}.groups")
		else:
			allowed_groups = None

		enabled = str(raw_rule.get("enabled", "启用")).strip()
		# 停用规则仍须通过上面的群范围校验，但不进入消息匹配缓存。
		if enabled != "启用":
			continue

		# 回复类型缺省按文本处理；类型不在白名单内的规则直接淘汰，避免运行期再分支
		reply_type = str(raw_rule.get("reply_type", REPLY_TYPE_TEXT)).strip() or REPLY_TYPE_TEXT
		if reply_type not in ALLOWED_REPLY_TYPES:
			logger.warning(f"[KeywordAutoReply] 未知 reply_type `{reply_type}`，已跳过该规则")
			continue

		pattern = str(raw_rule.get("pattern", "")).strip()
		reply_content = str(raw_rule.get("reply_content", "")).strip()

		# 保留既有规则筛选方式：缺少匹配或回复内容的规则不参与发送。
		if not pattern or not reply_content:
			continue

		# 持有编译结果，避免规则数超过标准库缓存容量时逐消息重新编译。
		# 来源：Python 正则文档 https://docs.python.org/3/library/re.html#re.compile
		compiled_pattern: re.Pattern[str] | str
		try:
			compiled_pattern = re.compile(pattern)
		except re.error as exc:
			# 只保留错误说明；仍在对应群消息处理时记录并跳过，不保留异常回溯占用内存。
			compiled_pattern = str(exc)

		normalized_rules.append(
			{
				"pattern": pattern,
				"compiled_pattern": compiled_pattern,
				"reply_type": reply_type,
				"reply_content": reply_content,
				"description": str(raw_rule.get("description", "")).strip(),
				"allowed_groups": allowed_groups,
			}
		)

	return normalized_rules


def _load_image_as_base64(image_path: str) -> str:
	"""读取本地图片并返回 base64 字符串。

	路径不存在或不是文件时直接抛出 FileNotFoundError；读取失败抛 OSError。
	上层负责捕获并打日志，不在此处吞掉异常。
	"""
	if not os.path.isfile(image_path):
		raise FileNotFoundError(f"图片文件不存在: {image_path}")
	with open(image_path, "rb") as f:
		return base64.b64encode(f.read()).decode("utf-8")


class KeywordAutoReplyHandler(BaseEventHandler):
	"""对群消息进行正则匹配并立即回复。"""

	event_type = EventType.ON_MESSAGE
	handler_name = "keyword_auto_reply_handler"
	handler_description = "对每条消息执行正则匹配，命中后立即发送预设回复"
	rules_cache = RuleCache((), frozenset())

	async def execute(self, message: MaiMessages | None) -> Tuple[bool, bool, Optional[str], None, Optional[MaiMessages]]:
		"""按群范围和正则处理 message，发送首条可回复规则，并返回执行结果五元组。

		发送文本或图片会调用框架并存储消息；返回值第二项始终允许框架继续处理。
		"""
		if not message:
			return True, True, "消息为空，跳过处理", None, message

		if not message.is_group_message:
			return True, True, "非群聊消息，跳过处理", None, message

		stream_id = message.stream_id or ""
		if not stream_id:
			return True, True, "缺少 stream_id，跳过处理", None, message

		group_id = str(message.message_base_info.get("group_id", "")).strip()
		if not group_id:
			return True, True, "缺少 group_id，跳过处理", None, message

		if not self.get_config("keyword_auto_reply.enable", True):
			return True, True, "插件未启用，跳过处理", None, message

		cache = self.__class__.rules_cache
		if not cache.rules:
			return True, True, "未配置有效规则，跳过处理", None, message

		# 覆盖集合来自全部规则而不是默认名单，可安全跳过无候选群且不影响规则覆盖。
		if cache.covered_groups is not None and group_id not in cache.covered_groups:
			return True, True, "当前群没有可触发的规则，跳过处理", None, message

		plain_text = (message.plain_text or "").strip()
		if not plain_text:
			return True, True, "消息纯文本为空，跳过处理", None, message

		# 逐条筛选群范围；不允许当前群的规则不能阻断后续规则，成功发送后才结束匹配。
		for rule in cache.rules:
			allowed_groups = rule["allowed_groups"]
			# 空集合应拒绝全部，不能用集合真假值把它误判为无限制。
			if allowed_groups is not None and group_id not in allowed_groups:
				continue

			pattern = rule["pattern"]
			compiled_pattern = rule["compiled_pattern"]
			# 字符串仅代表加载时已确认的正则语法错误，保留原有逐消息记录并跳过的行为。
			if isinstance(compiled_pattern, str):
				logger.error(f"[KeywordAutoReply] 非法正则 `{pattern}`: {compiled_pattern}")
				continue
			# 当前群通过范围筛选后才匹配，直接复用编译结果，不查询标准库的有界缓存。
			if not compiled_pattern.search(plain_text):
				continue

			reply_type = rule["reply_type"]
			reply_content = rule["reply_content"]

			# 根据规则类型分发：图片类型即时把本地路径读取为 base64 后通过框架发送
			if reply_type == REPLY_TYPE_IMAGE:
				try:
					image_base64 = _load_image_as_base64(reply_content)
				except (FileNotFoundError, OSError) as exc:
					# 路径错误只跳过当前规则，避免影响后续规则匹配
					logger.error(
						f"[KeywordAutoReply] 命中规则但读取图片失败，pattern={pattern}, "
						f"path={reply_content}: {exc}"
					)
					continue
				success = await self.send_image(
					stream_id=stream_id,
					image_base64=image_base64,
					storage_message=True,
				)
			else:
				success = await self.send_text(
					stream_id=stream_id,
					text=reply_content,
					storage_message=True,
				)

			# 保留发送失败即结束本次匹配的行为，避免后续规则再发送其他内容。
			if not success:
				logger.error(
					f"[KeywordAutoReply] 规则命中但发送失败，group_id={group_id}, "
					f"pattern={pattern}, reply_type={reply_type}"
				)
				return False, True, f"命中规则 {pattern}，但发送失败", None, message

			logger.info(
				f"[KeywordAutoReply] 命中规则并已回复({reply_type})，group_id={group_id}, "
				f"pattern={pattern}, description={rule.get('description', '')}"
			)
			return True, True, f"命中规则 {pattern}，已发送回复", None, message

		return True, True, "未命中任何规则", None, message


@register_plugin
class KeywordAutoReplyPlugin(BasePlugin):
	"""提供原生插件配置与事件组件，在注册时建立经过群范围校验的规则缓存。"""

	plugin_name = "keyword_auto_reply_plugin"
	enable_plugin = True
	dependencies = []
	python_dependencies = []
	config_file_name = "config.toml"

	config_section_descriptions = {
		"keyword_auto_reply": "关键词自动回复配置",
	}


	config_schema = {
		"keyword_auto_reply": {
			"enable": ConfigField(type=bool, default=True, description="是否启用关键词自动回复插件"),
			"default_groups": ConfigField(
				type=list,
				item_type="string",
				default=[],
				required=True,
				description="默认群白名单，仅供继承默认的规则使用。留空时这些规则不触发；群号只能包含 ASCII 数字。",
			),
			"rules": ConfigField(
				type=list,
				item_type="object",
				item_fields={
					"enabled": {
						"type": "select",
						"label": "是否启用该规则",
						"choices": ["启用", "不启用"],
						"default": "启用",
					},
					"group_scope": {
						"type": "select",
						"label": "生效范围",
						"choices": list(GROUP_SCOPES),
						"default": GROUP_SCOPE_DEFAULT,
					},
					"groups": {
						"type": "string",
						"label": "指定群聊名单（仅指定群聊模式使用）",
						"placeholder": "用英文逗号分隔，如 123456,654321；不加空格，留空不触发",
						"default": "",
					},
					"reply_type": {
						"type": "select",
						"label": "回复类型",
						"choices": [REPLY_TYPE_TEXT, REPLY_TYPE_IMAGE],
						"default": REPLY_TYPE_TEXT,
					},
					"pattern": {
						"type": "string",
						"label": "正则表达式",
						"placeholder": "输入用于匹配消息的正则表达式",
					},
					"reply_content": {
						"type": "string",
						"label": "回复内容",
						"placeholder": "文本类型填发送的文字；图片类型填本地图片路径",
					},
					"description": {
						"type": "string",
						"label": "规则描述",
						"placeholder": "可选，仅用于区分规则用途",
					},
				},
				default=[
					{
						"enabled": "启用",
						"group_scope": GROUP_SCOPE_DEFAULT,
						"groups": "",
						"reply_type": REPLY_TYPE_TEXT,
						"pattern": r"你好|hello",
						"reply_content": "你好。",
						"description": "基础问候回复",
					}
				],
				description=(
					"规则列表。按顺序检查群范围和正则；成功发送后停止匹配。"
					"指定群聊覆盖默认名单，所有群聊不受默认名单限制。保存配置后需重新加载插件。"
					"图片类型时回复内容填写本地图片路径（建议绝对路径）。"
				),
			),
		}
	}

	def get_plugin_components(self) -> List[Tuple[EventHandlerInfo, type[BaseEventHandler]]]:
		"""校验已加载的配置，成功后替换处理器类缓存并返回组件信息；配置错误直接抛出。"""
		config = self.config.get("keyword_auto_reply")
		if not isinstance(config, dict):
			raise ValueError("keyword_auto_reply 必须是配置节")
		if "default_groups" not in config:
			raise ValueError("keyword_auto_reply.default_groups 缺少必填字段")
		default_groups = _parse_group_ids(config["default_groups"], "keyword_auto_reply.default_groups")
		rules = _normalize_rules(config.get("rules", []), default_groups)
		# 校验期间不修改共享缓存，避免后面的无效规则留下只更新了一部分的名单。
		KeywordAutoReplyHandler.rules_cache = _build_rule_cache(rules)
		return [(KeywordAutoReplyHandler.get_handler_info(), KeywordAutoReplyHandler)]
