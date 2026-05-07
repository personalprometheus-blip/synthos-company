# PAGE_TEMPLATE_STANDARD — synthos_monitor pages

How to add a new page to `synthos_monitor.py` (Pi4B :5050) so the
header, navigation, color scheme, and refresh discipline match every
other page in the command portal.

## The pattern at a glance

Every page is a plain HTML file in `templates/` (no Jinja2
inheritance). The shared header is **injected at render time** via the
`_subpage_header(page_name)` helper, which produces a sticky header
with the wordmark, page subtitle, ET clock, "← Monitor" back link,
and the hamburger menu. The page only declares the body and its own
content styles.

## Wiring a new page

1. **Add the route** in `synthos_monitor.py`. The route renders the
   template and passes the header HTML in via `subpage_hdr`:

   ```python
   @app.route("/my-new-page")
   def my_new_page():
       if not _authorized():
           return redirect(url_for("login"))
       return render_template("my_new_page.html",
                              subpage_hdr=_subpage_header("My New Page"))
   ```

2. **Add the link** to the hamburger menu inside `_subpage_header()`.
   The menu lives near line 4499 in `synthos_monitor.py` — keep it
   alphabetised within its grouping.

3. **Create the template** at `templates/my_new_page.html`. Use the
   skeleton in the next section.

4. **Document any new `/api/*` endpoint** the page calls in the docstring
   of its handler so the next person can find it.

## Required CSS contract

These CSS variables are set by every page and read by helpers / older
pages — keep names + values stable:

```css
:root {
  --bg:       #080b12;
  --surface:  #0d1120;
  --surface2: #111827;
  --border:   rgba(255,255,255,0.07);
  --border2:  rgba(255,255,255,0.12);
  --text:     rgba(255,255,255,0.88);
  --muted:    rgba(255,255,255,0.35);
  --dim:      rgba(255,255,255,0.15);
  --teal:     #00f5d4;
  --pink:     #ff4b6e;
  --purple:   #7b61ff;
  --amber:    #ffb347;
  --mono:     'JetBrains Mono', monospace;
  --sans:     'Inter', sans-serif;
}
```

Page-level classes the rest of the portal expects:

| Class            | Use |
|------------------|-----|
| `.page`          | Main container, `max-width:1200px`, `padding:24px` |
| `.title`         | Page heading; can wrap part in `<span>` for the purple→teal gradient |
| `.subtitle`      | Description under the title |
| `.stats-row`     | KPI grid at the top of the page |
| `.stat-mini`     | Single KPI card |
| `.panel`         | Content panel (rounded, bordered, surface bg) |
| `.panel-header`  | Title bar inside a panel |
| `.panel-title`   | Uppercase 10px title text |
| `.panel-badge`   | Optional count/status badge in the panel header |
| `.empty`         | Empty-state placeholder inside a panel |
| `.error-bar`     | Inline error banner (pink, 8px radius) |
| `.loading`       | Loading placeholder |
| `.footer`        | Refresh-time line at the bottom |

The severity badge classes (`.sev-critical`, `.sev-high`,
`.sev-medium`, `.sev-low`) match the lowercase severity values from
the `auditor.db.detected_issues` table — use those exact names if
your page surfaces alerts.

## Skeleton template

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Synthos — My Page</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root {
  --bg:#080b12;--surface:#0d1120;--surface2:#111827;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:rgba(255,255,255,0.88);--muted:rgba(255,255,255,0.35);--dim:rgba(255,255,255,0.15);
  --teal:#00f5d4;--pink:#ff4b6e;--purple:#7b61ff;--amber:#ffb347;
  --mono:'JetBrains Mono',monospace;--sans:'Inter',sans-serif;
}
html,body{min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
.page{max-width:1200px;margin:0 auto;padding:24px}
.title{font-size:22px;font-weight:700;margin-bottom:4px}
.subtitle{font-size:12px;color:var(--muted);margin-bottom:20px}
.panel{border-radius:16px;border:1px solid var(--border);background:var(--surface);overflow:hidden;margin-bottom:16px}
.panel-header{padding:14px 16px;border-bottom:1px solid var(--border)}
.panel-title{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:var(--muted)}
.error-bar{padding:12px 16px;font-size:11px;color:var(--pink);background:rgba(255,75,110,0.06);border-radius:8px;margin-bottom:16px}
.loading{padding:24px;text-align:center;color:var(--muted);font-size:11px}
.footer{max-width:1200px;margin:24px auto 0;padding:16px 24px;border-top:1px solid var(--border);text-align:right;font-size:10px;color:var(--muted);font-family:var(--mono)}
</style>
</head>
<body>

{{ subpage_hdr|safe }}

<div class="page">
  <div class="title">My Page</div>
  <div class="subtitle">One-line description. Auto-refresh every 60s.</div>

  <div id="err-bar" class="error-bar" style="display:none"></div>

  <div class="panel">
    <div class="panel-header">
      <div class="panel-title">Section</div>
    </div>
    <div id="content"><div class="loading">Loading...</div></div>
  </div>
</div>

<div class="footer">Last refresh: <span id="refresh-time">—</span></div>

<script>
function showErr(msg) {
  const bar = document.getElementById('err-bar');
  bar.textContent = msg;
  bar.style.display = 'block';
  setTimeout(() => { bar.style.display = 'none'; }, 8000);
}

async function load() {
  try {
    const r = await fetch('/api/my-endpoint');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    document.getElementById('content').textContent = JSON.stringify(data, null, 2);
    document.getElementById('refresh-time').textContent = new Date().toLocaleTimeString();
  } catch (e) {
    showErr('Load failed: ' + e.message);
  }
}

load();
setInterval(load, 60000);
</script>
</body>
</html>
```

## Frontend ↔ API contract — verify before shipping

The single biggest source of "page renders but is empty" bugs is
the JS reading fields that don't match the actual API response. Before
you call a page done:

1. `curl -s http://127.0.0.1:5050/api/<endpoint> | python3 -m json.tool`
   — capture the actual response.
2. Map every field your JS reads to a real key in that response.
   Severity strings, timestamp formats (epoch int vs ISO vs RFC), and
   nested-vs-flat shapes are the usual mismatches.
3. With the page open in the browser, watch the network tab and
   confirm the data drives the DOM. `is-active` on the service is not
   evidence the page works.

## Don't-do list

- **No fake fallback data.** If the source DB or JSON is empty, return
  empty arrays / nulls and let the page render "—" or "no data". Never
  substitute hardcoded examples — silent demo data has burned us
  before.
- **No bypassing Mac-first → Pi git pull flow** for files that live in
  the synthos repo. `synthos-company` is edit-on-pi4b only; everything
  else goes Mac → GitHub → Pi.
- **No new templates that `extends "base.html"`.** The retail portal
  (`synthos_build/src/templates/`) uses inheritance and is a separate
  Flask app — don't cross-pollinate.
