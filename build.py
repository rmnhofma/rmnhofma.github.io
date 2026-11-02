#!/usr/bin/python3
"""Generates the site from the public <Name>-release repositories on GitHub.

Every public repository of OWNER named <Name>-release with a published release becomes a plugin:
a card on index.html and a page in plugins/<name>/ with its README, release notes, images and a
PDF of its manual. There is nothing to configure: run it after a release and commit the result.
"""

import hashlib
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

OWNER = "rmnhofma"
SITE_URL = f"https://{OWNER}.github.io"
ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
PLUGINS = ROOT / "plugins"
HASHES = ROOT / ".manual-hashes.json"

# Source repositories are private: nothing on the site may link one (release-repo.md in FrameworkBtw).
PRIVATE_LINK = re.compile(rf"{OWNER}/[A-Za-z]+Btw([^-\w]|$)|FrameworkBtw", re.M)
FORMATS_LINE = re.compile(r"\s*CLAP and VST3 for Linux\.\s*$")
IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
LINK_ATTR = re.compile(r'(href|src)="([^"]*)"')
TAG = re.compile(r"<[^>]+>")

env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))


def github_token():
    if token := os.environ.get("GITHUB_TOKEN"):
        return token
    if shutil.which("gh"):
        done = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        return done.stdout.strip() if done.returncode == 0 else None
    return None


TOKEN = github_token()


def api(path):
    """A GET on the GitHub API; the token only lifts the rate limit, everything read is public."""
    request = urllib.request.Request(f"https://api.github.com/{path}")
    request.add_header("Accept", "application/vnd.github+json")
    if TOKEN:
        request.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def checkout(repo):
    """The repository's default branch as it is on GitHub, in a shallow clone under .cache/."""
    path = CACHE / repo["name"]
    branch = repo["default_branch"]
    if path.exists():
        subprocess.run(["git", "-C", path, "fetch", "-q", "--depth", "1", "origin", branch], check=True)
        subprocess.run(["git", "-C", path, "reset", "-q", "--hard", "FETCH_HEAD"], check=True)
    else:
        CACHE.mkdir(exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", "--branch", branch, repo["clone_url"], path], check=True
        )
    return path


def render_markdown(text):
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "sane_lists", "toc"], extension_configs={"toc": {"toc_depth": "2-2"}}
    )
    return md.convert(text), md.toc


def rewrite_links(rendered, rewrite):
    """Applies rewrite(target) to every relative link and image in rendered HTML."""

    def replace(match):
        attr, target = match.group(1), html.unescape(match.group(2))
        if re.match(r"[a-z]+:|#", target):
            return match.group(0)
        return f'{attr}="{html.escape(rewrite(target))}"'

    return LINK_ATTR.sub(replace, rendered)


def repo_link(repo, target):
    return f"{repo['html_url']}/blob/{repo['default_branch']}/{target}"


def page_link(repo, name, target):
    """A README's relative link, seen from the plugin's page."""
    if target == "manual.md":
        return f"{name}-manual.pdf"
    if target.startswith("README.md"):
        return target.removeprefix("README.md") or "./"
    if target.startswith("images/"):
        return target
    return repo_link(repo, target)


def manual_link(repo, name, target):
    """A manual's relative link, seen from the PDF: images stay files, the rest must work anywhere."""
    if target.startswith("images/"):
        return target
    if target.startswith("README.md"):
        return f"{SITE_URL}/plugins/{name.lower()}/" + target.removeprefix("README.md")
    return repo_link(repo, target)


def image_size(path):
    with open(path, "rb") as file:
        header = file.read(24)
    return struct.unpack(">II", header[16:24]) if header[:4] == b"\x89PNG" else (None, None)


def parse_readme(text, name):
    """Splits a README into what the page's top shows (summary, main image) and the rest."""
    lines = text.split("\n")
    if not lines[0].startswith("# "):
        sys.exit(f"{name}: the README doesn't start with '# {name}'")
    rest = "\n".join(lines[1:]).strip()
    first, _, rest = rest.partition("\n\n")
    summary_html, _ = render_markdown(FORMATS_LINE.sub("", first))
    summary_html = re.sub(r"^<p>|</p>$", "", summary_html)
    image = IMAGE.search(rest)
    if not image:
        sys.exit(f"{name}: the README has no screenshot")
    body = rest[: image.start()] + rest[image.end() :]
    return {"summary": summary_html, "alt": image.group(1), "image": image.group(2), "body": body.strip()}


def parse_notes(body, repo):
    """A release's notes, without the install paragraph every release repeats and the page already has."""
    paragraphs = [p for p in (body or "").strip().split("\n\n") if not p.startswith("Install:")]
    notes = "\n\n".join(paragraphs).replace("github.com/PluginsBTW/", f"github.com/{OWNER}/")
    rendered, _ = render_markdown(notes)
    return rendered


def parse_release(release, repo):
    published = datetime.fromisoformat(release["published_at"].replace("Z", "+00:00"))
    assets = {asset["name"]: asset for asset in release["assets"]}
    archive = next((asset for n, asset in assets.items() if n.endswith(".tar.gz")), None)
    checksum = assets.get(f"{archive['name']}.sha256") if archive else None
    return {
        "version": release["tag_name"],
        "published": published,
        "date": f"{published.day} {published:%b %Y}",
        "url": release["html_url"],
        "notes": Markup(parse_notes(release["body"], repo)),
        "download": archive["browser_download_url"] if archive else release["html_url"],
        "size": f"{archive['size'] / 1_000_000:.1f} MB" if archive else None,
        "checksum": checksum["browser_download_url"] if checksum else None,
    }


def gallery(manual, source, main_image):
    """The manual's other screenshots, once each."""
    seen = {main_image}
    shots = []
    for alt, target in IMAGE.findall(manual):
        if target not in seen and (source / target).exists():
            seen.add(target)
            width, height = image_size(source / target)
            shots.append({"src": target, "alt": alt, "width": width, "height": height})
    return shots


def collect(repo):
    name = repo["name"].removesuffix("-release")
    releases = [r for r in api(f"repos/{repo['full_name']}/releases?per_page=100") if not (r["draft"] or r["prerelease"])]
    if not releases:
        print(f"  {name}: skipped, no published release yet")
        return None
    source = checkout(repo)
    readme = parse_readme((source / "README.md").read_text(), name)
    manual = (source / "manual.md").read_text()
    width, height = image_size(source / readme["image"])
    parsed = [parse_release(r, repo) for r in releases]
    latest = parsed[0]
    if latest["version"].lstrip("v") not in (source / "README.md").read_text():
        print(f"  warning: {name}'s README doesn't mention its latest version, {latest['version']}")
    body, _ = render_markdown(readme["body"])
    return {
        "name": name,
        "slug": name.lower(),
        "repo": repo,
        "source": source,
        "summary": Markup(readme["summary"]),
        "description": html.unescape(TAG.sub("", readme["summary"])),
        "image": readme["image"],
        "alt": readme["alt"],
        "width": width,
        "height": height,
        "body": Markup(rewrite_links(body, lambda t: page_link(repo, name, t))),
        "manual": manual,
        "gallery": gallery(manual, source, readme["image"]),
        "pdf": f"{name}-manual.pdf",
        "latest": latest,
        "earlier": parsed[1:],
    }


def write_checked(path, content):
    if match := PRIVATE_LINK.search(content):
        sys.exit(f"{path}: links something private: {match.group(0)!r}")
    path.write_text(content)


def build_manual(plugin, out, hashes):
    """manual.md to a PDF with Chromium, redone only when its inputs changed: a PDF differs per run."""
    name, source = plugin["name"], plugin["source"]
    body, toc = render_markdown(plugin["manual"])
    body = rewrite_links(body, lambda t: manual_link(plugin["repo"], name, t))
    body = body.replace("<h2", toc + "<h2", 1)
    page = env.get_template("manual.html").render(
        title=f"{name} manual",
        body=Markup(body),
        css=Markup((ROOT / "templates" / "manual.css").read_text()),
        logo=(ROOT / "assets" / "logo-mark.svg").as_uri(),
        font=(ROOT / "assets" / "fonts" / "Ubuntu-Light.ttf").as_uri(),
    )
    if match := PRIVATE_LINK.search(page):
        sys.exit(f"{name}'s manual links something private: {match.group(0)!r}")

    digest = hashlib.sha256(page.encode())
    for image in sorted((source / "images").iterdir()):
        digest.update(image.read_bytes())
    digest = digest.hexdigest()
    pdf = out / plugin["pdf"]
    if hashes.get(name) == digest and pdf.exists():
        return
    print(f"  {name}: printing the manual")
    # In the clone, so that images and the table of contents' anchors resolve.
    temporary = source / ".manual.html"
    temporary.write_text(page)
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(
            [
                "chromium",
                "--headless=new",
                "--disable-gpu",
                f"--user-data-dir={profile}",
                "--no-pdf-header-footer",
                "--generate-pdf-document-outline",
                f"--print-to-pdf={pdf}",
                temporary.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
    temporary.unlink()
    hashes[name] = digest


def build_plugin(plugin, hashes):
    out = PLUGINS / plugin["slug"]
    out.mkdir(parents=True, exist_ok=True)
    images = out / "images"
    if images.exists():
        shutil.rmtree(images)
    shutil.copytree(plugin["source"] / "images", images)
    build_manual(plugin, out, hashes)
    write_checked(
        out / "index.html",
        env.get_template("plugin.html").render(plugin=plugin, root="../../", site_url=SITE_URL, owner=OWNER),
    )


def main():
    repos = api(f"users/{OWNER}/repos?per_page=100&type=owner")
    repos = [r for r in repos if r["name"].endswith("-release") and not r["private"] and not r["archived"]]
    print(f"{len(repos)} release repositories")
    plugins = [plugin for repo in repos if (plugin := collect(repo))]
    plugins.sort(key=lambda plugin: plugin["latest"]["published"], reverse=True)

    hashes = json.loads(HASHES.read_text()) if HASHES.exists() else {}
    for plugin in plugins:
        build_plugin(plugin, hashes)
    current = {plugin["slug"] for plugin in plugins}
    if PLUGINS.exists():
        for old in PLUGINS.iterdir():
            if old.name not in current:
                print(f"  removing {old.name}: its repository is gone or has no release")
                shutil.rmtree(old)
    hashes = {plugin["name"]: hashes[plugin["name"]] for plugin in plugins}
    HASHES.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n")
    write_checked(
        ROOT / "index.html", env.get_template("index.html").render(plugins=plugins, root="", site_url=SITE_URL, owner=OWNER)
    )
    for plugin in plugins:
        print(f"  {plugin['name']} {plugin['latest']['version']} ({plugin['latest']['date']})")


if __name__ == "__main__":
    main()
