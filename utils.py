import datetime
import html
import os

def get_datetime_str() -> str:
    now = datetime.datetime.now()
    return now.strftime("%Y%m%d%H%M%S")

DEFAULT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>batch_name</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 20px; background: #0f172a; color: #f8fafc; }
        h1 { color: #38bdf8; text-align: center; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; background: #1e293b; border-radius: 8px; overflow: hidden; }
        th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #334155; }
        th { background: #0284c7; color: #ffffff; }
        tr:hover { background: #334155; }
        a { color: #38bdf8; text-decoration: none; word-break: break-all; }
        a:hover { text-decoration: underline; color: #7dd3fc; }
    </style>
</head>
<body>
    <h1>batch_name</h1>
    <table>
        <thead>
            <tr><th>Content Title & Link</th></tr>
        </thead>
        <tbody>
            tbody_content
        </tbody>
    </table>
</body>
</html>"""

def create_html_file(file_name: str, batch_name: str, contents: list) -> None:
    tbody = ''
    for line in contents:
        line_str = str(line).strip()
        if not line_str:
            continue
        if ':' in line_str:
            parts = line_str.split(':', 1)
            text = parts[0].strip()
            url = parts[1].strip()
        else:
            text = line_str
            url = line_str
        safe_text = html.escape(text)
        safe_url = html.escape(url)
        tbody += f'<tr><td><a href="{safe_url}" target="_blank">{safe_text}</a></td></tr>\n'

    template_path = os.path.join(os.path.dirname(__file__), 'template.html')
    if os.path.exists(template_path):
        with open(template_path, 'r', encoding='utf-8') as fp:
            file_content = fp.read()
    else:
        file_content = DEFAULT_HTML_TEMPLATE

    rendered = file_content.replace('tbody_content', tbody).replace('batch_name', html.escape(batch_name))
    with open(file_name, 'w', encoding='utf-8') as fp:
        fp.write(rendered)
