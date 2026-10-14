import copy
import json
import shutil

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Application, AppRevision
from app.schemas import AgentOptions
from app.services import skills
from .conftest import post


@pytest.fixture
def skill_root(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    shutil.copytree(skills.ROOT, root)
    monkeypatch.setattr(skills, "ROOT", root)
    return root


def write_skill(root, name="example", frontmatter="", body="Read the evidence."):
    target = root / name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\nname: {name}\ndescription: Example method\n{frontmatter}---\n{body}")
    return target


def test_default_skills_have_compatible_adapters_and_match_configuration():
    catalog = {skill["name"]: skill for skill in skills.catalog()}
    assert set(catalog) == set(AgentOptions().skills)
    assert catalog["sympy"]["compatibility"]["status"] == "limited"
    assert "仅支持求导、积分、极限" in catalog["sympy"]["runtime_note"]
    assert "外部示意图服务" in catalog["scientific-critical-thinking"]["runtime_note"]
    assert catalog["sympy"]["upstream"]["allowed-tools"] == "Read Write Edit Bash"
    for item in catalog.values():
        assert set(item["requirements"]) <= skills.SUPPORTED_TOOLS
        assert item["compatibility"]["missing_tools"] == []
        assert "instructions" not in item


def test_published_snapshot_keeps_body_references_and_adapter_when_files_change(skill_root):
    snapshot = skills.snapshot_skills(["sympy"])
    snapshot = json.loads(json.dumps(snapshot))  # Exact storage round trip.
    expected = skills.load_skill("sympy", snapshot)
    ref = skills.read_reference("sympy", "references/review.md", snapshot)
    (skill_root / "sympy" / "SKILL.md").write_text("invalid replacement")
    (skill_root / "sympy" / "references" / "review.md").write_text("new reference")
    (skill_root / "runtime.json").write_text("invalid replacement")
    assert skills.load_skill("sympy", snapshot) == expected
    assert skills.read_reference("sympy", "references/review.md", snapshot) == ref
    assert skills.runtime_catalog(snapshot)[0]["sha256"] == expected["sha256"]
    assert skills.snapshot_info(snapshot)["pinned"] is True
    shutil.rmtree(skill_root)
    assert skills.load_skill("sympy", snapshot) == expected


def test_reference_only_update_changes_package_and_release_hash(skill_root):
    old = skills.snapshot_skills(["sympy"])
    ref = skill_root / "sympy" / "references" / "review.md"
    ref.write_text(ref.read_text() + "\nA new method note.\n")
    new = skills.snapshot_skills(["sympy"])
    assert old["skills"]["sympy"]["sha256"] == new["skills"]["sympy"]["sha256"]
    assert old["skills"]["sympy"]["package_sha256"] != new["skills"]["sympy"]["package_sha256"]
    assert old["sha256"] != new["sha256"]
    assert skills.snapshot_skills(["sympy", "sympy"]) == new


def test_only_published_skills_and_references_are_available(skill_root):
    snapshot = skills.snapshot_skills(["sympy"])
    (skill_root / "sympy" / "references" / "later.md").write_text("not published")
    with pytest.raises(ValueError, match="发布版本"):
        skills.load_skill("course-evidence", snapshot)
    with pytest.raises(ValueError, match="发布版本"):
        skills.read_reference("sympy", "references/later.md", snapshot)
    assert {item["name"] for item in skills.runtime_catalog(snapshot)} == {"sympy"}
    loaded = skills.load_skill("sympy", snapshot)
    loaded["compatibility"]["status"] = "changed by caller"
    assert skills.load_skill("sympy", snapshot)["compatibility"]["status"] == "limited"


def test_legacy_releases_have_explicit_live_version_notice():
    assert skills.runtime_catalog(None) == skills.catalog()
    assert skills.snapshot_info(None) == {
        "pinned": False, "source": "legacy_live", "sha256": None,
        "note": "此应用发布于技能版本固定功能之前；本次使用当前技能文件，重新发布后固定版本。",
    }


def test_missing_dependencies_and_unadapted_external_tools_block_publishing(skill_root):
    write_skill(skill_root, frontmatter="requirements: [run_shell]\n")
    item = next(item for item in skills.catalog() if item["name"] == "example")
    assert item["compatibility"] == {"status": "incompatible", "missing_tools": ["run_shell"]}
    with pytest.raises(ValueError, match="缺少工具 run_shell"):
        skills.snapshot_skills(["example"])
    write_skill(skill_root, frontmatter="allowed-tools: Read Write Bash\n")
    with pytest.raises(ValueError, match="不兼容"):
        skills.snapshot_skills(["example"])
    with pytest.raises(ValueError, match="缺少工具 calculate"):
        skills.snapshot_skills(["sympy"], available_tools=skills.SUPPORTED_TOOLS - {"calculate"})


def test_optional_subagents_are_not_required_by_any_default_skill():
    snapshot = skills.snapshot_skills(AgentOptions().skills, available_tools=skills.SUPPORTED_TOOLS - {"spawn_agent"})
    assert len(skills.runtime_catalog(snapshot)) == 5


def test_snapshot_detects_changed_body_reference_and_manifest(skill_root):
    original = skills.snapshot_skills(["sympy"])
    for key in ("instructions", "runtime_note"):
        modified = copy.deepcopy(original)
        modified["skills"]["sympy"][key] += " changed"
        with pytest.raises(ValueError, match="校验失败"):
            skills.runtime_catalog(modified)
    modified = copy.deepcopy(original)
    modified["skills"]["sympy"]["references"]["references/review.md"]["content"] += " changed"
    with pytest.raises(ValueError, match="校验失败"):
        skills.read_reference("sympy", "references/review.md", modified)


@pytest.mark.parametrize("path", ["../SKILL.md", "/etc/passwd", "references/../../other.md",
                                   "references/../SKILL.md", "references//test.md", "references/test.py",
                                   "references\\test.md", "references/./test.md", "references/\x00.md"])
def test_reference_path_safety_in_live_and_published_packages(path):
    snapshot = skills.snapshot_skills(["sympy"])
    for source in (None, snapshot):
        with pytest.raises(ValueError):
            skills.read_reference("sympy", path, source)


def test_symlink_escape_and_cross_skill_reference_are_rejected(skill_root, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("private")
    ref = skill_root / "sympy" / "references" / "escape.md"
    for target in (secret, skill_root / "course-evidence" / "SKILL.md"):
        ref.symlink_to(target)
        with pytest.raises(ValueError, match="路径不允许"):
            skills.read_reference("sympy", "references/escape.md")
        with pytest.raises(ValueError, match="路径不允许"):
            skills.snapshot_skills(["sympy"])
        ref.unlink()


def test_snapshot_total_bytes_and_reference_count_are_bounded(skill_root, monkeypatch):
    snapshot = skills.snapshot_skills(["sympy"])
    monkeypatch.setattr(skills, "MAX_SNAPSHOT_BYTES", 1000)
    with pytest.raises(ValueError, match="总大小超限"):
        skills.snapshot_skills(["sympy"])
    with pytest.raises(ValueError, match="总大小超限"):
        skills.runtime_catalog(snapshot)
    monkeypatch.setattr(skills, "MAX_SNAPSHOT_BYTES", 1_000_000)
    monkeypatch.setattr(skills, "MAX_REFERENCES", 1)
    with pytest.raises(ValueError, match="数量过多"):
        skills.snapshot_skills(["sympy"])


def test_frontmatter_supports_bom_crlf_and_dashes_inside_yaml(skill_root):
    path = write_skill(skill_root)
    path.write_bytes(b'\xef\xbb\xbf---\r\nname: example\r\ndescription: "before---after"\r\n---\r\nmethod\r\n---\r\nend')
    skill = skills.load_skill("example")
    assert skill["description"] == "before---after"
    assert skill["instructions"] == "method\n---\nend"


@pytest.mark.parametrize("frontmatter", ["requirements: run_shell\n", "requirements: [1]\n",
                                         "requirements: [[calculate]]\n", "description: duplicate\n",
                                         "metadata: malformed\n", "compatibility: [wrong]\n",
                                         "metadata: {version: [wrong]}\n"])
def test_malformed_frontmatter_is_rejected_without_granting_tools(skill_root, frontmatter):
    write_skill(skill_root, frontmatter=frontmatter)
    with pytest.raises(ValueError):
        skills.snapshot_skills(["example"])


def test_publication_pins_skill_packages_until_next_publish(client, platform, skill_root):
    app = platform["bundles"][0]["app"]
    draft = {**app["draft"], "agent": {"enabled": True, "skills": ["sympy"]}}
    assert client.put(f"/api/admin/apps/{app['id']}", json=draft).status_code == 200
    assert post(client, f"/admin/apps/{app['id']}/publish")["state"] == "succeeded"
    with Session(engine()) as db:
        record = db.get(Application, app["id"])
        version = record.version
        old = copy.deepcopy(record.published["skill_snapshot"])
        revision = db.scalar(select(AppRevision).where(AppRevision.app_id == app["id"], AppRevision.version == version))
        assert revision.configuration["skill_snapshot"] == old
    body = skill_root / "sympy" / "SKILL.md"
    reference = skill_root / "sympy" / "references" / "review.md"
    body.write_text(body.read_text() + "\nNew method instructions.\n")
    reference.write_text(reference.read_text() + "\nNew reference version.\n")
    with Session(engine()) as db:
        assert db.get(Application, app["id"]).published["skill_snapshot"] == old
    assert post(client, f"/admin/apps/{app['id']}/publish")["state"] == "succeeded"
    with Session(engine()) as db:
        new = db.get(Application, app["id"]).published["skill_snapshot"]
        assert new["sha256"] != old["sha256"]
        revision = db.scalar(select(AppRevision).where(AppRevision.app_id == app["id"], AppRevision.version == version))
        assert revision.configuration["skill_snapshot"] == old
    assert "New method instructions" not in skills.load_skill("sympy", old)["instructions"]
    assert "New method instructions" in skills.load_skill("sympy", new)["instructions"]
    assert "New reference version" not in skills.read_reference("sympy", "references/review.md", old)
    assert "New reference version" in skills.read_reference("sympy", "references/review.md", new)


def test_api_rejects_enabled_skill_with_unavailable_tool(client, platform, skill_root):
    app = platform["bundles"][0]["app"]
    write_skill(skill_root, frontmatter="requirements: [run_shell]\n")
    draft = {**app["draft"], "agent": {"enabled": True, "skills": ["example"]}}
    response = client.put(f"/api/admin/apps/{app['id']}", json=draft)
    assert response.status_code == 422
    assert "run_shell" in response.json()["detail"]
