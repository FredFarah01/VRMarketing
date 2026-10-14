"""Build a read-only public preview of the actual Jinja landing page."""
from pathlib import Path
import shutil
from jinja2 import Environment, FileSystemLoader

root = Path(__file__).resolve().parent
out = root / "preview-public"
(out / "static" / "css").mkdir(parents=True, exist_ok=True)
(out / "static" / "js").mkdir(parents=True, exist_ok=True)
(out / "static" / "img").mkdir(parents=True, exist_ok=True)
(out / "static" / "videos").mkdir(parents=True, exist_ok=True)
env = Environment(loader=FileSystemLoader(str(root / "templates")), autoescape=True)
env.globals["url_for"] = lambda endpoint, filename=None, **kw: "/static/" + filename if endpoint == "static" else "/"
env.globals["csrf_token"] = lambda: "preview-only"
html = env.get_template("landing.html").render(
    canonical="https://veridyn-sales-staging.onrender.com/care-recruitment-compliance-checklist",
    job_roles=["Owner / Director", "Registered Manager", "Recruitment Manager", "Compliance Manager", "HR", "Operations", "Administrator", "Other"],
    org_sizes=["1–25", "26–50", "51–100", "101–250", "251–500", "500+"],
    video_ad_ready=True)
# Prevent test submissions or misleading form data collection on static preview.
html = html.replace('action="/api/leads" method="post"', 'action="#" method="post" onsubmit="event.preventDefault();alert(\'Preview only — lead capture is disabled here.\');"')
html = html.replace('<body class="page-landing">', '<body class="page-landing"><div style="position:relative;z-index:999;background:#fff1cc;color:#4c3900;text-align:center;padding:10px;font:600 14px sans-serif">PUBLIC PREVIEW — video and layout only. Forms are disabled; use the production site for enquiries.</div>')
html = html.replace('<script src="/static/js/main.js" defer></script>', '')
html = html.replace('href="/demo"', 'href="https://veridyn-sales-staging.onrender.com/demo"')
(out / "index.html").write_text(html, encoding="utf-8")
for path in ["css/styles.css", "img/favicon.svg"]:
    src = root / "static" / path
    if src.exists():
        shutil.copy2(src, out / "static" / path)
video = root / "static/videos/Veridyn_Recruit_Screens_50s_With_Audio.mp4"
if not video.exists():
    raise SystemExit("Uploaded video not found")
shutil.copy2(video, out / "static/videos" / video.name)
print("Built full landing page preview with uploaded video:", video.stat().st_size, "bytes")
