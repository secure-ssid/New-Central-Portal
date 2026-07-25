"""Guards against the Tailwind theme silently vanishing from the build.

The built stylesheet was generated with no tailwind.config.js at all, so it
contained none of the brand-*/surface-* utilities while the templates used them
~320 times. Because USE_BUILT_TAILWIND=1 in the container, every one of those
classes was a no-op in production: pages carried by the inline design system
(.card, .btn-*, .tbl) looked right and pages using surface-* panels did not.
That is the failure this file exists to catch.
"""
import pathlib
import re

import pytest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
TEMPLATES = APP / "templates"
BUILT_CSS = APP / "static" / "dist" / "tailwind.css"
THEME_JS = APP / "static" / "tailwind-theme.js"
CONFIG_JS = APP / "tailwind.config.js"

TOKEN_RE = re.compile(r"[a-z]*:?[a-z-]*(?:surface|brand)-\d+(?:/\d+)?")


def _tokens_used() -> set[str]:
    found = set()
    for f in TEMPLATES.rglob("*.html"):
        found.update(TOKEN_RE.findall(f.read_text()))
    return found


def _css_selector(token: str) -> str:
    return "." + token.replace(":", r"\:").replace("/", r"\/")


def test_config_and_theme_files_exist():
    assert CONFIG_JS.exists(), "tailwind.config.js is what makes the theme reach the build"
    assert THEME_JS.exists(), "the theme must live in one file both build paths read"


def test_config_reads_the_shared_theme():
    """Dev (Play CDN) and prod (CLI build) must not be able to drift apart."""
    assert "tailwind-theme" in CONFIG_JS.read_text()


def test_build_paths_pass_the_config():
    dockerfile = (APP / "Dockerfile").read_text()
    script = (APP.parent / "scripts" / "build-tailwind.sh").read_text()
    assert "--config tailwind.config.js" in dockerfile
    assert "--config tailwind.config.js" in script


def test_base_html_no_longer_carries_its_own_theme():
    """The duplicated inline tailwind.config is what the CLI build never saw."""
    base = (TEMPLATES / "base.html").read_text()
    assert "tailwind.config = {" not in base


@pytest.mark.skipif(not BUILT_CSS.exists(), reason="tailwind.css not built")
def test_every_theme_class_used_by_a_template_exists_in_the_built_css():
    css = BUILT_CSS.read_text()
    used = _tokens_used()
    assert used, "expected the templates to use brand-*/surface-* utilities"
    missing = sorted(t for t in used if _css_selector(t) not in css)
    assert not missing, (
        f"{len(missing)} theme utilities are used in templates but absent from "
        f"the built CSS (rebuild with scripts/build-tailwind.sh): {missing}"
    )


@pytest.mark.skipif(not BUILT_CSS.exists(), reason="tailwind.css not built")
def test_built_css_carries_the_palette_values():
    """A build that lost the config would still emit CSS — just without these."""
    css = BUILT_CSS.read_text()
    # brand-500 #f97316 -> rgb(249 115 22), surface-700 #1e2535 -> rgb(30 37 53)
    assert "249 115 22" in css, "brand-500 missing from built CSS"
    assert "30 37 53" in css, "surface-700 missing from built CSS"


def test_theme_defines_every_shade_the_templates_reference():
    theme = THEME_JS.read_text()
    shades = {t.split("-")[-2] + "-" + t.split("-")[-1].split("/")[0]
              if False else re.search(r"(surface|brand)-(\d+)", t).groups()
              for t in _tokens_used()}
    for family, shade in sorted(shades):
        # e.g. `300: "#fdba74"` inside the brand or surface block
        assert re.search(rf"{shade}:\s*[\"']#", theme), (
            f"{family}-{shade} is used in a template but not defined in "
            f"tailwind-theme.js"
        )


def test_design_system_is_an_external_stylesheet():
    """Keeping it inline made it uncacheable and re-sent on every page load."""
    base = (TEMPLATES / "base.html").read_text()
    assert '/static/app.css' in base
    assert (APP / "static" / "app.css").exists()


def test_standalone_pages_share_the_design_system():
    """login/404/500 each used to restate the whole theme, and it had drifted."""
    for name in ("login.html", "errors/404.html", "errors/500.html"):
        html = (TEMPLATES / name).read_text()
        assert "/static/app.css" in html, f"{name} does not use the shared stylesheet"
        assert "<style>" not in html, f"{name} still carries an inline theme"


def test_no_page_loads_an_external_subresource():
    """Self-hosted assets: no CDN round trips, and nothing for the production
    reverse proxy's CSP to block.

    Only <link>/<script>/<img>/<iframe> count — plain <a href> to vendor
    documentation is a hyperlink the user clicks, not something the page loads.
    """
    tag_re = re.compile(
        r'<(?:link|script|img|iframe)\b[^>]*\b(?:src|href)="(https?://[^"]+)"',
        re.I,
    )
    offenders = []
    for f in TEMPLATES.rglob("*.html"):
        for m in tag_re.finditer(f.read_text()):
            offenders.append(f"{f.name}: {m.group(1)}")
    assert not offenders, f"external subresources loaded: {offenders}"


def test_no_template_uses_a_class_defined_only_in_another_pages_style_block():
    """Catches an orphaned class across the whole template tree.

    A page-scoped <style> block is invisible to every other page, so a class
    borrowed from one renders as unstyled text. That shipped once: the
    compliance board used .topo-badge, which is defined inside topology.html's
    scoped block, so "Update available" rendered as plain text.
    """
    shared_css = (APP / "static" / "app.css").read_text()
    built_css = BUILT_CSS.read_text() if BUILT_CSS.exists() else ""

    # Collect the classes each page defines privately, and where.
    private: dict[str, str] = {}
    for template in TEMPLATES.rglob("*.html"):
        text = template.read_text()
        for block in re.findall(r"<style>(.*?)</style>", text, re.S):
            for name in re.findall(r"\.([a-z][a-z0-9-]{2,})\s*[,{:]", block):
                private.setdefault(name, template.name)

    offenders = []
    for template in TEMPLATES.rglob("*.html"):
        own_blocks = "".join(re.findall(r"<style>(.*?)</style>",
                                        template.read_text(), re.S))
        for group in re.findall(r'class="([^"]*)"', template.read_text()):
            for name in group.split():
                if "{{" in name or "{%" in name or name not in private:
                    continue
                if private[name] == template.name:
                    continue          # defined on this very page
                if f".{name}" in own_blocks or f".{name}" in shared_css:
                    continue          # also available globally
                if f".{name}" in built_css:
                    continue          # a Tailwind utility
                offenders.append(f"{template.name} uses .{name} "
                                 f"(defined only in {private[name]})")
    assert not offenders, offenders
