#!/usr/bin/env python3
"""专家包自检：对齐《WorkBuddy 专家开发规范 v2.4》§10.1 一致性约束与 §10.2 自检清单。

本包的两处具体处理（都是本包的实际情况）：
  - M 组：本专家不依赖任何 MCP，未声明 `dependencies.mcpServers` 视为通过；
  - P18b：技能正文不在 expert/ 内维护，源码侧检查兄弟目录 `../skill/SKILL.md` 存在。

用法：
    python3 scripts/check_package.py                  # 只查源码目录
    python3 scripts/check_package.py --zip <zip路径>   # 同时查打包产物

退出码：0 全部通过；1 有失败项。只读，不修改任何文件。
"""
import json
import os
import re
import struct
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_SRC = os.path.join(os.path.dirname(ROOT), "skill")
FAIL, OK = [], []

CATEGORY_IDS = {
    "01-ProductDesign", "02-Engineering", "03-GameSpatial", "04-DataAI",
    "05-MarketingGrowth", "06-ContentCreative", "07-SalesCommerce",
    "08-FinanceInvestment", "09-OperationsHR", "10-ProjectQuality",
    "11-SecurityCompliance", "12-IndustryConsultant", "13-TencentZone",
    "14-WorldWise", "15-Education",
}
CJK = re.compile(r"[\u4e00-\u9fff]")
SECRET = re.compile(
    r"(AKID[0-9A-Za-z]{10,}|secret[_-]?key[\"']?\s*[:=]\s*\S{8,}|Bearer\s+[A-Za-z0-9._-]{20,})")


def check(cond, code, msg):
    (OK if cond else FAIL).append("{} {}".format(code, msg))


def localized_ok(value):
    """v2.4 §6.3 LocalizedString：纯字符串或 {zh, en} 双语对象，双语时两种都得填。"""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value.get("zh", "").strip()) and bool(value.get("en", "").strip())
    return False


def norm(rel):
    """去掉开头的 ./，但不要像 lstrip 那样把 .mcp.json 的点和下划线也吃掉。"""
    return rel[2:] if rel.startswith("./") else rel


def parse_frontmatter(path):
    text = open(path, encoding="utf-8").read()
    if not text.startswith("---"):
        return {}, text
    end = text.index("\n---", 3)
    meta, parent = {}, None
    for raw in text[3:end].splitlines():
        if not raw.strip():
            continue
        if raw.startswith("  - ") and parent:
            meta.setdefault(parent, [])
            if isinstance(meta[parent], dict):
                meta[parent] = []
            meta[parent].append(raw[4:].strip())
            continue
        if raw.startswith("  ") and parent:
            k, _, v = raw.strip().partition(":")
            if isinstance(meta.get(parent), dict):
                meta[parent][k.strip()] = v.strip().strip('"')
            continue
        k, _, v = raw.partition(":")
        k, v = k.strip(), v.strip().strip('"')
        if v == "":
            meta[k] = {}
            parent = k
        else:
            meta[k] = v
            parent = k
    return meta, text


def scan_secrets(paths):
    hits = []
    for base in paths:
        if not os.path.exists(base):
            continue
        for dirpath, _, files in os.walk(base):
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    text = open(p, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                if SECRET.search(text):
                    hits.append(os.path.relpath(p, ROOT))
    return hits


def main():
    zip_path = sys.argv[sys.argv.index("--zip") + 1] if "--zip" in sys.argv else None

    plugin_path = os.path.join(ROOT, ".codebuddy-plugin", "plugin.json")
    if not os.path.exists(plugin_path):
        print("缺少 .codebuddy-plugin/plugin.json")
        return 1
    plugin = json.load(open(plugin_path, encoding="utf-8"))

    # ---- 目录结构 ----
    for d in ("agents", "avatars"):
        check(os.path.isdir(os.path.join(ROOT, d)), "S-{}".format(d), "{}/ 在插件根目录".format(d))
    for d in ("agents", "skills", "bin"):
        check(not os.path.exists(os.path.join(ROOT, ".codebuddy-plugin", d)),
              "S-nested", ".codebuddy-plugin/ 下没有 {}".format(d))
    check(os.path.exists(os.path.join(ROOT, "README.md")), "S-readme", "README.md 存在")

    # ---- plugin.json 基础字段 ----
    name = plugin.get("name", "")
    check(bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name)), "P1", "name 为小写字母+连字符：{}".format(name))
    check(bool(re.fullmatch(r"\d+\.\d+\.\d+", plugin.get("version", ""))), "P2",
          "version 语义化：{}".format(plugin.get("version")))
    desc = plugin.get("description", "")
    check(bool(desc) and not CJK.search(desc), "P3", "description 为英文一句话")
    author = plugin.get("author", {})
    check(bool(author.get("name")) and bool(author.get("email")), "P4", "author 含 name 与 email")
    check(plugin.get("expertType") in ("agent", "team"), "P6", "expertType = {}".format(plugin.get("expertType")))

    # ---- agents / skills 路径数组 ----
    agents = plugin.get("agents", [])
    check(isinstance(agents, list) and len(agents) >= 1, "P5a", "agents 为路径数组：{}".format(agents))
    agent_meta = {}
    for rel in agents:
        p = os.path.join(ROOT, norm(rel))
        check(os.path.exists(p), "P5b", "agents 路径存在：{}".format(rel))
        if os.path.exists(p):
            agent_meta[os.path.basename(p)[:-3]] = parse_frontmatter(p)[0]

    agent_name = plugin.get("agentName", "")
    check(agent_name in agent_meta, "P7", "agentName 对应 agents/ 下的 MD：{}".format(agent_name))

    skills = plugin.get("skills", [])
    check(isinstance(skills, list) and len(skills) >= 1, "P18a", "skills 为路径数组：{}".format(skills))
    for rel in skills:
        built = os.path.join(ROOT, "build", name, norm(rel))
        in_build = os.path.exists(os.path.join(built, "SKILL.md"))
        in_src = os.path.exists(os.path.join(SKILL_SRC, "SKILL.md"))
        check(in_build or in_src, "P18b",
              "{}/SKILL.md 存在（build 产物或兄弟目录 skill/）".format(rel))

    # ---- 展示字段 ----
    dn = plugin.get("displayName", {})
    check(localized_ok(dn), "P8a", "displayName 中英文都已填写")
    check(0 < len(dn.get("zh", "")) <= 15, "P8b", "displayName.zh 长度 {}（平台校验 ≤15）".format(len(dn.get("zh", ""))))
    check(localized_ok(plugin.get("profession", {})), "P9", "profession 中英文都已填写")
    check(plugin.get("profession", {}).get("zh") != dn.get("zh"), "P9b", "profession 与 displayName 不撞名")
    dd = plugin.get("displayDescription", {})
    check(localized_ok(dd), "P10a", "displayDescription 中英文都已填写")
    check(40 <= len(dd.get("zh", "")) <= 50, "P10b",
          "displayDescription.zh 长度 {}（需 40—50）".format(len(dd.get("zh", ""))))
    check(plugin.get("avatar") == "avatars/expert.png", "P11a", "avatar = {}".format(plugin.get("avatar")))
    check(os.path.exists(os.path.join(ROOT, plugin.get("avatar", ""))), "P11b", "avatar 路径指向存在的文件")
    check(plugin.get("categoryId") in CATEGORY_IDS, "P12", "categoryId 在分类列表内：{}".format(plugin.get("categoryId")))
    check(plugin.get("plugin") == name, "P14", "plugin 与 name 一致：{}".format(plugin.get("plugin")))

    # ---- 提示词与标签 ----
    dip = plugin.get("defaultInitPrompt", {})
    check(localized_ok(dip), "P13", "defaultInitPrompt 中英文都已填写")

    tags = plugin.get("tags", [])
    check(3 <= len(tags) <= 5, "P15a", "tags 数量 {}（需 3—5）".format(len(tags)))
    check(all(localized_ok(t) for t in tags), "P15b", "tags 每项中英文都已填写")

    qp = plugin.get("quickPrompts", [])
    check(len(qp) == 3, "P16a", "quickPrompts 恰好 3 个（实际 {}）".format(len(qp)))
    check(all(localized_ok(q) for q in qp), "P16b", "quickPrompts 每项中英文都已填写")
    if qp:
        check(dip.get("zh") == qp[0].get("zh") and dip.get("en") == qp[0].get("en"),
              "P17", "defaultInitPrompt 与 quickPrompts[0] 逐字一致")

    # ---- Agent MD ----
    for stem, meta in agent_meta.items():
        check(meta.get("name") == stem, "A1", "frontmatter name = 文件名：{}".format(stem))
        ad = meta.get("description", "")
        check(bool(ad) and not CJK.search(ad), "A2", "{} 的 description 为英文".format(stem))
        check("tools" not in meta, "A3", "{} 的 frontmatter 不含 tools".format(stem))
        check(localized_ok(meta.get("displayName", {})), "A4", "{} 的 displayName 中英文都已填写".format(stem))
        check(localized_ok(meta.get("profession", {})), "A5", "{} 的 profession 中英文都已填写".format(stem))
        if "maxTurns" in meta:
            check(str(meta["maxTurns"]).isdigit(), "A6", "{} 的 maxTurns 为整数".format(stem))
        if "skills" in meta:
            check(isinstance(meta["skills"], list), "A7", "{} 的 skills 为列表".format(stem))
        for lang in ("zh", "en"):
            check(meta.get("displayName", {}).get(lang) == dn.get(lang),
                  "A8", "displayName.{} 与 plugin.json 一致".format(lang))

    # ---- 头像 ----
    av = os.path.join(ROOT, plugin.get("avatar", ""))
    if os.path.exists(av):
        data = open(av, "rb").read()
        is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
        is_jpg = data[:2] == b"\xff\xd8"
        check(is_png or is_jpg, "V1", "头像为 PNG 或 JPG")
        if is_png:
            w, h = struct.unpack(">II", data[16:24])
            check((w, h) == (512, 512), "V2", "头像尺寸 {}x{}（需 512x512）".format(w, h))
        check(len(data) <= 500 * 1024, "V3", "头像大小 {} 字节（需 ≤500KB）".format(len(data)))
    else:
        check(False, "V0", "头像文件存在（先跑 scripts/make_avatar.py）")

    # ---- 依赖 ----
    deps = plugin.get("dependencies", {})
    mcp_decl = deps.get("mcpServers")
    if mcp_decl is None:
        check(True, "M1", "未声明 MCP 依赖（本专家全部本地完成，符合预期）")
    elif isinstance(mcp_decl, str):
        check(os.path.exists(os.path.join(ROOT, norm(mcp_decl))), "M1a",
              "dependencies.mcpServers 指向存在的文件：{}".format(mcp_decl))
    else:
        check(False, "M1b", "dependencies.mcpServers 既未声明也不是路径字符串")

    hits = scan_secrets([os.path.join(ROOT, "agents"), os.path.join(ROOT, ".codebuddy-plugin")])
    check(not hits, "M6", "包内无硬编码 Token / 密钥（命中 {}）".format(hits[:3]))

    # ---- ZIP ----
    if zip_path:
        check(os.path.exists(zip_path), "Z0", "ZIP 存在：{}".format(zip_path))
        if os.path.exists(zip_path):
            with zipfile.ZipFile(zip_path) as z:
                names = z.namelist()
                need = ["{}/.codebuddy-plugin/plugin.json".format(name),
                        "{}/{}".format(name, plugin.get("avatar", ""))]
                need += ["{}/{}".format(name, norm(rel)) for rel in agents]
                need += ["{}/{}/SKILL.md".format(name, norm(rel)) for rel in skills]
                for n in need:
                    check(n in names, "Z1", "ZIP 含 {}".format(n))
                roots = {n.split("/")[0] for n in names if "/" in n}
                check(roots == {name}, "Z2", "ZIP 顶层目录唯一且等于 name：{}".format(roots))
                junk = [n for n in names if any(f in n for f in
                                                (".DS_Store", "__MACOSX", "/._", ".git/", "__pycache__"))]
                check(not junk, "Z3", "ZIP 无系统垃圾文件（命中 {}）".format(junk[:3]))
                # 本包自定义：技能必须带得上参考文档与两个脚本，否则装上跑不起来
                for rel in skills:
                    base = "{}/{}".format(name, norm(rel))
                    for sub in ("references/renderers.md", "scripts/prepare_window.py",
                                "scripts/check_ledger.py"):
                        check("{}/{}".format(base, sub) in names, "Z4",
                              "ZIP 含技能文件 {}".format(sub))
                    dev = [n for n in names if n.startswith(base + "/fixtures")
                           or n.endswith("/test-prompts.json") or n.endswith("/selftest.sh")
                           or n.endswith("/vendor_wecom.sh")]
                    check(not dev, "Z5", "ZIP 不含开发期产物（命中 {}）".format(dev[:3]))

    for line in OK:
        print("PASS", line)
    for line in FAIL:
        print("FAIL", line)
    print("\n通过 {} 项，失败 {} 项".format(len(OK), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
