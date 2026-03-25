from __future__ import annotations

import subprocess

import update_manager as um


def test_parse_repo_slug_supports_common_remote_url_forms():
    cases = {
        "git@github.com:ibarnard/YdxbotV2.git": "ibarnard/YdxbotV2",
        "git@github.com:ibarnard/YdxbotV2": "ibarnard/YdxbotV2",
        "https://github.com/ibarnard/YdxbotV2": "ibarnard/YdxbotV2",
        "https://github.com/ibarnard/YdxbotV2.git": "ibarnard/YdxbotV2",
        "https://token@github.com/ibarnard/YdxbotV2.git": "ibarnard/YdxbotV2",
        "https://github.com/ibarnard/YdxbotV2/": "ibarnard/YdxbotV2",
        "ssh://git@github.com/ibarnard/YdxbotV2.git": "ibarnard/YdxbotV2",
        "git://github.com/ibarnard/YdxbotV2.git": "ibarnard/YdxbotV2",
    }
    for remote_url, expected in cases.items():
        assert um._parse_repo_slug(remote_url) == expected


def test_parse_repo_slug_rejects_non_github_remote():
    assert um._parse_repo_slug("git@gitlab.com:ibarnard/YdxbotV2.git") is None


def test_extract_github_token_from_remote_url():
    url = "https://ibarnard:token_example_1234567890@github.com/ibarnard/YdxbotV2.git"
    assert um._extract_github_token_from_remote(url) == "token_example_1234567890"


def test_resolve_github_token_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("YDXBOT_GITHUB_TOKEN", "token_env_value")
    token = um.resolve_github_token(str(tmp_path), "")
    assert token == "token_env_value"


def test_resolve_github_token_from_global_config(monkeypatch, tmp_path):
    monkeypatch.delenv("YDXBOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "global_config.json").write_text(
        """{
  # release auth for private repo
  "update": {
    "github_token": "token_cfg_value"
  }
}
""",
        encoding="utf-8",
    )
    token = um.resolve_github_token(str(tmp_path), "")
    assert token == "token_cfg_value"


def test_get_latest_release_gives_private_repo_hint_on_auth_errors(monkeypatch):
    class DummyResp:
        status_code = 404

        def json(self):
            return {}

    monkeypatch.setattr(um.requests, "get", lambda *args, **kwargs: DummyResp())
    result = um.get_latest_release("ibarnard/YdxbotV2")
    assert result["success"] is False
    assert "私有仓库请配置 GitHub Token" in result["error"]


def test_get_blocking_dirty_paths_ignores_runtime_artifacts(monkeypatch, tmp_path):
    status_stdout = "\n".join(
        [
            "?? .DS_Store",
            "?? tests_multiuser/users/tim/config.json",
            "?? users/shuji/session.session",
            " M users/shuji/state.json",
            "?? user/legacy_user/config.py",
            " M global_config.json",
            " M config/global_config.json",
            "?? global.json",
            "?? nohup.out",
            " M users/shuji/presets.json",
            "?? zq_multiuser.py",
        ]
    )

    def fake_run_cmd(args, cwd, timeout=30):
        if args[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=status_stdout, stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(um, "_run_cmd", fake_run_cmd)
    blocking = um.get_blocking_dirty_paths(str(tmp_path))
    assert blocking == ["users/shuji/presets.json", "zq_multiuser.py"]


def test_list_version_catalog_contains_pending_and_summary(monkeypatch, tmp_path):
    summaries = {
        "v1.0.9": "v1.0.9: fix updater command",
        "v1.0.8": "v1.0.8: yc preset refresh",
        "v1.0.7": "v1.0.7: lose-end style",
    }
    dates = {
        "v1.0.9": "2026-02-24",
        "v1.0.8": "2026-02-24",
        "v1.0.7": "2026-02-23",
    }

    def fake_run_cmd(args, cwd, timeout=30):
        if args == ["git", "config", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="https://github.com/ibarnard/YdxbotV2.git\n", stderr="")
        if args == ["git", "fetch", "--tags", "origin"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="abcdef1234567890\n", stderr="")
        if args == ["git", "branch", "--show-current"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="main\n", stderr="")
        if args == ["git", "describe", "--tags", "--exact-match"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="v1.0.7\n", stderr="")
        if args == ["git", "describe", "--tags", "--abbrev=0"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="v1.0.7\n", stderr="")
        if args == ["git", "tag", "--list", "v*", "--sort=-version:refname"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="v1.0.9\nv1.0.8\nv1.0.7\n", stderr="")
        if len(args) == 5 and args[:4] == ["git", "log", "-1", "--format=%cs"]:
            tag = args[4]
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=dates.get(tag, "") + "\n", stderr="")
        if len(args) == 4 and args[:2] == ["git", "for-each-ref"] and args[3] == "--format=%(subject)":
            tag = args[2].replace("refs/tags/", "")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=summaries.get(tag, "") + "\n", stderr="")
        if len(args) == 5 and args[:4] == ["git", "log", "-1", "--format=%s"]:
            tag = args[4]
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=summaries.get(tag, "") + "\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(um, "_run_cmd", fake_run_cmd)
    catalog = um.list_version_catalog(str(tmp_path), limit=3)

    assert catalog["success"] is True
    assert catalog["latest_tag"] == "v1.0.9"
    assert catalog["pending_tags"] == ["v1.0.9", "v1.0.8"]
    assert catalog["entries"][0]["summary"] == "v1.0.9: fix updater command"


def test_update_to_version_without_target_uses_latest_tag(monkeypatch):
    monkeypatch.setattr(
        um,
        "list_version_catalog",
        lambda repo_root=None, limit=1: {"success": True, "latest_tag": "v1.0.9"},
    )
    monkeypatch.setattr(
        um,
        "update_to_ref",
        lambda repo_root=None, target_ref=None: {"success": True, "target_ref": target_ref, "after": {"display_version": "v1.0.9"}},
    )

    result = um.update_to_version("/tmp/repo", "")
    assert result["success"] is True
    assert result["resolved_target"] == "v1.0.9"


def test_reback_to_version_requires_target():
    result = um.reback_to_version("/tmp/repo", "")
    assert result["success"] is False
    assert "请提供目标版本号或提交" in result["error"]


def test_run_health_check_skips_missing_legacy_files(monkeypatch, tmp_path):
    (tmp_path / "verify_deps.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "main_multiuser.py").write_text("x=1\n", encoding="utf-8")
    (tmp_path / "zq_multiuser.py").write_text("x=1\n", encoding="utf-8")
    (tmp_path / "user_manager.py").write_text("x=1\n", encoding="utf-8")
    (tmp_path / "model_manager.py").write_text("x=1\n", encoding="utf-8")
    (tmp_path / "update_manager.py").write_text("x=1\n", encoding="utf-8")
    (tmp_path / "constants.py").write_text("x=1\n", encoding="utf-8")

    recorded = {"verify": 0, "compile_args": []}

    def fake_run_cmd(args, cwd, timeout=30):
        if args == [um.sys.executable, "verify_deps.py"]:
            recorded["verify"] += 1
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[:3] == [um.sys.executable, "-m", "py_compile"]:
            recorded["compile_args"] = args[3:]
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(um, "_run_cmd", fake_run_cmd)
    result = um.run_health_check(str(tmp_path))

    assert result["success"] is True
    assert recorded["verify"] == 1
    assert "main.py" not in recorded["compile_args"]
    assert "zq.py" not in recorded["compile_args"]
    assert "main_multiuser.py" in recorded["compile_args"]
    assert "zq_multiuser.py" in recorded["compile_args"]
    assert "model_manager.py" in recorded["compile_args"]
    assert "update_manager.py" in recorded["compile_args"]
    assert "verify_deps.py" in recorded["compile_args"]
    assert "constants.py" in recorded["compile_args"]
