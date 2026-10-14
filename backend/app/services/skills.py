"""Read-only Skills, capability adapters, and immutable application-release snapshots.

Metadata describes dependencies; it never grants tools or filesystem permissions.
"""
import copy
import hashlib
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1] / "skills"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
FRONT_MATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.S)
MAX_FILE_BYTES = 100_000
MAX_SNAPSHOT_BYTES = 1_000_000
MAX_SKILLS = 64
MAX_REFERENCES = 128
SUPPORTED_TOOLS = frozenset({"search_knowledge", "read_skill", "read_skill_reference", "calculate", "spawn_agent"})
READ_ONLY_NOTE = "仅可调用应用提供的工具；Skill 中的脚本、安装命令、文件写入和外部服务示例不授予执行权限。"


class _MetadataLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in keys:
                    raise ValueError("Skill 元数据包含重复字段")
                keys.add(key)
            except TypeError as exc:
                raise ValueError("Skill 元数据字段必须是标量") from exc
        return super().construct_mapping(node, deep=deep)


def _read(path):
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()) or not resolved.is_file():
        raise ValueError("Skill 文件不存在或路径不允许")
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Skill 文件过大")
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Skill 文件无法读取为 UTF-8 文本") from exc


def _name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("无效 Skill 名称")
    return name


def _requirements(value):
    if not isinstance(value, list) or len(value) > 32 or any(
        not isinstance(name, str) or not TOOL_NAME.fullmatch(name) for name in value
    ):
        raise ValueError("Skill requirements 必须是工具名称列表")
    return sorted(set(value))


def _encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Skill 快照不是有效 JSON 数据") from exc


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _adapters():
    path = ROOT / "runtime.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(_read(path))
    except json.JSONDecodeError as exc:
        raise ValueError("Skill 运行时适配配置格式错误") from exc
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("skills"), dict):
        raise ValueError("Skill 运行时适配配置版本或格式错误")
    return value["skills"]


def _compatibility(skill, available_tools=None):
    supported = SUPPORTED_TOOLS if available_tools is None else set(available_tools)
    missing = sorted(set(_requirements(skill["requirements"])) - supported)
    status = skill["compatibility"]["status"]
    if not isinstance(status, str) or status not in {"compatible", "limited", "incompatible"}:
        raise ValueError("Skill 兼容性状态无效")
    return {"status": "incompatible" if missing else status, "missing_tools": missing}


def _load_local(name, adapters=None):
    _name(name)
    source = _read(ROOT / name / "SKILL.md")
    match = FRONT_MATTER.fullmatch(source)
    if not match:
        raise ValueError("Skill 缺少有效 YAML 元数据")
    try:
        metadata = yaml.load(match[1], Loader=_MetadataLoader)
    except yaml.YAMLError as exc:
        raise ValueError("Skill 元数据格式错误") from exc
    if not isinstance(metadata, dict) or metadata.get("name") != name:
        raise ValueError("Skill 名称与目录不一致")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Skill 缺少说明")
    instructions = match[2].strip()
    if not instructions:
        raise ValueError("Skill 缺少方法正文")
    adapters = _adapters() if adapters is None else adapters
    adapter = adapters.get(name, {})
    if not isinstance(adapter, dict):
        raise ValueError("Skill 适配声明必须是对象")
    requirements = _requirements(metadata.get("requirements", []))
    requirements = sorted(set(requirements + _requirements(adapter.get("requirements", []))))
    note = adapter.get("runtime_note", READ_ONLY_NOTE)
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Skill 适配说明必须是文本")
    status = adapter.get("status", "compatible")
    # A reviewed adapter can retain methods while excluding unavailable examples.
    allowed_tools = metadata.get("allowed-tools", "")
    if isinstance(allowed_tools, list) and all(isinstance(tool, str) for tool in allowed_tools):
        allowed_tools = " ".join(allowed_tools)
    if not isinstance(allowed_tools, str):
        raise ValueError("Skill 上游工具声明必须是文本或名称列表")
    if not adapter and set(allowed_tools.split()) - SUPPORTED_TOOLS:
        status = "incompatible"
        note = "此 Skill 声明了平台之外的工具，需要管理员配置只读方法适配后才能启用。"
    upstream = {"allowed-tools": allowed_tools} if allowed_tools else {}
    if "compatibility" in metadata:
        upstream["compatibility"] = metadata["compatibility"]
    if any(not isinstance(value, str) for value in upstream.values()):
        raise ValueError("Skill 上游工具与兼容性说明必须是文本")
    version = metadata.get("metadata", {})
    if not isinstance(version, dict):
        raise ValueError("Skill metadata 字段必须是对象")
    version = version.get("version", "unversioned")
    if not isinstance(version, (str, int, float)) or isinstance(version, bool):
        raise ValueError("Skill 版本必须是文本或数字")
    skill = {"name": name, "description": description.strip(), "instructions": instructions,
             "sha256": hashlib.sha256(source.encode()).hexdigest(), "version": str(version),
             "requirements": requirements, "compatibility": {"status": status},
             "runtime_note": note, "upstream": upstream}
    skill["compatibility"] = _compatibility(skill)
    return skill


def _summary(skill):
    return {key: copy.deepcopy(value) for key, value in skill.items() if key not in {"instructions", "references"}}


def catalog():
    adapters = _adapters()
    return [_summary(_load_local(path.name, adapters)) for path in sorted(ROOT.iterdir())
            if path.is_dir() and (path / "SKILL.md").is_file()]


def _reference_path(name, path):
    _name(name)
    if not isinstance(path, str) or not path or len(path) > 200 or "\\" in path or "\x00" in path:
        raise ValueError("Skill 参考路径不允许")
    relative = Path(path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("Skill 参考路径不允许")
    if len(relative.parts) < 2 or relative.parts[0] != "references" or relative.suffix != ".md":
        raise ValueError("只能读取 Skill 的 references/*.md 参考资料")
    return relative.as_posix()


def read_reference(name, path, snapshot=None):
    path = _reference_path(name, path)
    if snapshot is not None:
        skill = _pinned(name, snapshot)
        if path not in skill["references"]:
            raise ValueError("Skill 参考资料未包含在此发布版本中")
        return skill["references"][path]["content"]
    base = (ROOT / name).resolve()
    target = (base / path).resolve()
    if not target.is_relative_to(base):
        raise ValueError("Skill 参考路径不允许")
    return _read(target)


def snapshot_skills(names, available_tools=None):
    """Validate selected packages and capture all readable references for publishing."""
    if not isinstance(names, (list, tuple)) or len(names) > MAX_SKILLS:
        raise ValueError("Skill 列表无效或数量过多")
    for name in names:
        _name(name)
    packages, total_bytes = {}, 0
    adapters = _adapters()
    for name in sorted(set(names)):
        skill = _load_local(name, adapters)
        skill["compatibility"] = _compatibility(skill, available_tools)
        _require_compatible(skill)
        references = {}
        for path in sorted((ROOT / name / "references").rglob("*.md")):
            if len(references) >= MAX_REFERENCES:
                raise ValueError("Skill 参考资料数量过多")
            relative = path.relative_to(ROOT / name).as_posix()
            content = read_reference(name, relative)
            total_bytes += len(content.encode())
            if total_bytes > MAX_SNAPSHOT_BYTES:
                raise ValueError("Skill 发布快照总大小超限")
            references[relative] = {"content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}
        skill["references"] = references
        skill["package_sha256"] = _hash(skill)
        packages[name] = skill
    payload = {"version": 1, "skills": packages}
    snapshot = {**payload, "sha256": _hash(payload)}
    if len(_encoded(snapshot)) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Skill 发布快照总大小超限")
    return snapshot


def _require_compatible(skill):
    compatibility = skill["compatibility"]
    if compatibility["status"] == "incompatible":
        reason = ("缺少工具 " + ", ".join(compatibility["missing_tools"]) if compatibility["missing_tools"]
                  else skill["runtime_note"])
        raise ValueError(f"Skill {skill['name']} 不兼容：{reason}")


def _validated(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1 or not isinstance(snapshot.get("skills"), dict):
        raise ValueError("Skill 发布快照格式或版本无效，请重新发布应用")
    if len(snapshot["skills"]) > MAX_SKILLS or len(_encoded(snapshot)) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Skill 发布快照总大小超限")
    if snapshot.get("sha256") != _hash({"version": 1, "skills": snapshot["skills"]}):
        raise ValueError("Skill 发布快照校验失败，请重新发布应用")
    for name, skill in snapshot["skills"].items():
        _name(name)
        if not isinstance(skill, dict) or skill.get("name") != name:
            raise ValueError("Skill 发布快照名称无效")
        required_strings = ("description", "instructions", "sha256", "runtime_note", "version")
        if any(not isinstance(skill.get(key), str) or not skill[key] for key in required_strings):
            raise ValueError("Skill 发布快照正文或元数据无效")
        if skill.get("package_sha256") != _hash({k: v for k, v in skill.items() if k != "package_sha256"}):
            raise ValueError("Skill 发布快照包校验失败")
        if not isinstance(skill.get("compatibility"), dict) or "status" not in skill["compatibility"]:
            raise ValueError("Skill 发布快照缺少兼容性声明")
        if "requirements" not in skill:
            raise ValueError("Skill 发布快照缺少工具依赖声明")
        _require_compatible({**skill, "compatibility": _compatibility(skill)})
        references = skill.get("references")
        if not isinstance(references, dict) or len(references) > MAX_REFERENCES:
            raise ValueError("Skill 发布快照参考资料无效")
        for path, reference in references.items():
            _reference_path(name, path)
            if not isinstance(reference, dict) or not isinstance(reference.get("content"), str):
                raise ValueError("Skill 发布快照参考资料无效")
            content = reference["content"].encode()
            if len(content) > MAX_FILE_BYTES or reference.get("sha256") != hashlib.sha256(content).hexdigest():
                raise ValueError("Skill 发布快照参考资料校验失败")
    return snapshot


def _pinned(name, snapshot):
    _name(name)
    packages = _validated(snapshot)["skills"]
    if name not in packages:
        raise ValueError("此 Skill 未包含在应用发布版本中")
    return packages[name]


def load_skill(name, snapshot=None):
    if snapshot is None:
        return _load_local(name)
    return copy.deepcopy({k: v for k, v in _pinned(name, snapshot).items() if k != "references"})


def runtime_catalog(snapshot):
    """None supports old releases; callers must record the legacy mode explicitly."""
    if snapshot is None:
        return catalog()
    return [_summary(skill) for skill in _validated(snapshot)["skills"].values()]


def snapshot_info(snapshot):
    if snapshot is None:
        return {"pinned": False, "source": "legacy_live", "sha256": None,
                "note": "此应用发布于技能版本固定功能之前；本次使用当前技能文件，重新发布后固定版本。"}
    return {"pinned": True, "source": "published", "sha256": _validated(snapshot)["sha256"],
            "note": "技能正文、参考资料和适配声明已随应用版本固定。"}
