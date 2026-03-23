"""
Report API路由
提供模拟报告生成、获取、对话等接口
"""

import os
import traceback
import threading
from flask import request, jsonify, send_file

from . import report_bp
from ..config import Config
from ..services.report_agent import ReportAgent, ReportManager, ReportStatus
from ..services.simulation_manager import SimulationManager
from ..models.project import ProjectManager
from ..models.task import TaskManager, TaskStatus
from ..utils.logger import get_logger

logger = get_logger('mirofish.api.report')


# ============== 报告生成接口 ==============

@report_bp.route('/generate', methods=['POST'])
def generate_report():
    """
    生成模拟分析报告（异步任务）
    
    这是一个耗时操作，接口会立即返回task_id，
    使用 GET /api/report/generate/status 查询进度
    
    请求（JSON）：
        {
            "simulation_id": "sim_xxxx",    // 必填，模拟ID
            "force_regenerate": false        // 可选，强制重新生成
        }
    
    返回：
        {
            "success": true,
            "data": {
                "simulation_id": "sim_xxxx",
                "task_id": "task_xxxx",
                "status": "generating",
                "message": "报告生成任务已启动"
            }
        }
    """
    try:
        data = request.get_json() or {}
        
        simulation_id = data.get('simulation_id')
        if not simulation_id:
            return jsonify({
                "success": False,
                "error": "请提供 simulation_id"
            }), 400
        
        force_regenerate = data.get('force_regenerate', False)
        
        # 获取模拟信息
        manager = SimulationManager()
        state = manager.get_simulation(simulation_id)
        
        if not state:
            return jsonify({
                "success": False,
                "error": f"模拟不存在: {simulation_id}"
            }), 404
        
        # 检查是否已有报告
        if not force_regenerate:
            existing_report = ReportManager.get_report_by_simulation(simulation_id)
            if existing_report and existing_report.status == ReportStatus.COMPLETED:
                return jsonify({
                    "success": True,
                    "data": {
                        "simulation_id": simulation_id,
                        "report_id": existing_report.report_id,
                        "status": "completed",
                        "message": "报告已存在",
                        "already_generated": True
                    }
                })
        
        # 获取项目信息
        project = ProjectManager.get_project(state.project_id)
        if not project:
            return jsonify({
                "success": False,
                "error": f"项目不存在: {state.project_id}"
            }), 404
        
        graph_id = state.graph_id or project.graph_id
        if not graph_id:
            return jsonify({
                "success": False,
                "error": "缺少图谱ID，请确保已构建图谱"
            }), 400

        simulation_requirement = project.simulation_requirement
        if not simulation_requirement:
            return jsonify({
                "success": False,
                "error": "缺少模拟需求描述"
            }), 400

        # Resolve user keys: project-level → server fallback (unless REQUIRE_USER_KEYS)
        _zep_key = project.user_zep_api_key or (None if Config.REQUIRE_USER_KEYS else Config.ZEP_API_KEY)
        _llm_key = project.user_llm_api_key or (None if Config.REQUIRE_USER_KEYS else Config.LLM_API_KEY)
        _llm_model = project.user_llm_model_name or Config.LLM_MODEL_NAME
        _llm_base_url = project.user_llm_base_url or (None if Config.REQUIRE_USER_KEYS else Config.LLM_BASE_URL)
        if not _zep_key or not _llm_key:
            return jsonify({"success": False, "error": "user_keys_required",
                            "message": "Please provide your API keys in the setup screen."}), 400
        
        # 提前生成 report_id，以便立即返回给前端
        import uuid
        report_id = f"report_{uuid.uuid4().hex[:12]}"
        
        # 创建异步任务
        task_manager = TaskManager()
        task_id = task_manager.create_task(
            task_type="report_generate",
            metadata={
                "simulation_id": simulation_id,
                "graph_id": graph_id,
                "report_id": report_id
            }
        )
        
        # 定义后台任务
        def run_generate():
            try:
                task_manager.update_task(
                    task_id,
                    status=TaskStatus.PROCESSING,
                    progress=0,
                    message="初始化Report Agent..."
                )
                
                # 创建Report Agent — inject user keys if provided
                from ..utils.llm_client import LLMClient
                from ..services.zep_tools import ZepToolsService
                _agent_llm = LLMClient(api_key=_llm_key, base_url=_llm_base_url, model=_llm_model)
                _agent_zep = ZepToolsService(api_key=_zep_key, llm_client=_agent_llm)
                agent = ReportAgent(
                    graph_id=graph_id,
                    simulation_id=simulation_id,
                    simulation_requirement=simulation_requirement,
                    llm_client=_agent_llm,
                    zep_tools=_agent_zep,
                )
                
                # 进度回调
                def progress_callback(stage, progress, message):
                    task_manager.update_task(
                        task_id,
                        progress=progress,
                        message=f"[{stage}] {message}"
                    )
                
                # 生成报告（传入预先生成的 report_id）
                report = agent.generate_report(
                    progress_callback=progress_callback,
                    report_id=report_id
                )
                
                # 保存报告
                ReportManager.save_report(report)
                
                if report.status == ReportStatus.COMPLETED:
                    task_manager.complete_task(
                        task_id,
                        result={
                            "report_id": report.report_id,
                            "simulation_id": simulation_id,
                            "status": "completed"
                        }
                    )
                else:
                    task_manager.fail_task(task_id, report.error or "报告生成失败")
                
            except Exception as e:
                logger.error(f"报告生成失败: {str(e)}")
                task_manager.fail_task(task_id, str(e))
        
        # 启动后台线程
        thread = threading.Thread(target=run_generate, daemon=True)
        thread.start()
        
        return jsonify({
            "success": True,
            "data": {
                "simulation_id": simulation_id,
                "report_id": report_id,
                "task_id": task_id,
                "status": "generating",
                "message": "报告生成任务已启动，请通过 /api/report/generate/status 查询进度",
                "already_generated": False
            }
        })
        
    except Exception as e:
        logger.error(f"启动报告生成任务失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@report_bp.route('/generate/status', methods=['POST'])
def get_generate_status():
    """
    查询报告生成任务进度
    
    请求（JSON）：
        {
            "task_id": "task_xxxx",         // 可选，generate返回的task_id
            "simulation_id": "sim_xxxx"     // 可选，模拟ID
        }
    
    返回：
        {
            "success": true,
            "data": {
                "task_id": "task_xxxx",
                "status": "processing|completed|failed",
                "progress": 45,
                "message": "..."
            }
        }
    """
    try:
        data = request.get_json() or {}
        
        task_id = data.get('task_id')
        simulation_id = data.get('simulation_id')
        
        # 如果提供了simulation_id，先检查是否已有完成的报告
        if simulation_id:
            existing_report = ReportManager.get_report_by_simulation(simulation_id)
            if existing_report and existing_report.status == ReportStatus.COMPLETED:
                return jsonify({
                    "success": True,
                    "data": {
                        "simulation_id": simulation_id,
                        "report_id": existing_report.report_id,
                        "status": "completed",
                        "progress": 100,
                        "message": "报告已生成",
                        "already_completed": True
                    }
                })
        
        if not task_id:
            return jsonify({
                "success": False,
                "error": "请提供 task_id 或 simulation_id"
            }), 400
        
        task_manager = TaskManager()
        task = task_manager.get_task(task_id)
        
        if not task:
            return jsonify({
                "success": False,
                "error": f"任务不存在: {task_id}"
            }), 404
        
        return jsonify({
            "success": True,
            "data": task.to_dict()
        })
        
    except Exception as e:
        logger.error(f"查询任务状态失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============== 报告获取接口 ==============

@report_bp.route('/<report_id>', methods=['GET'])
def get_report(report_id: str):
    """
    获取报告详情
    
    返回：
        {
            "success": true,
            "data": {
                "report_id": "report_xxxx",
                "simulation_id": "sim_xxxx",
                "status": "completed",
                "outline": {...},
                "markdown_content": "...",
                "created_at": "...",
                "completed_at": "..."
            }
        }
    """
    try:
        report = ReportManager.get_report(report_id)
        
        if not report:
            return jsonify({
                "success": False,
                "error": f"报告不存在: {report_id}"
            }), 404
        
        return jsonify({
            "success": True,
            "data": report.to_dict()
        })
        
    except Exception as e:
        logger.error(f"获取报告失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@report_bp.route('/by-simulation/<simulation_id>', methods=['GET'])
def get_report_by_simulation(simulation_id: str):
    """
    根据模拟ID获取报告
    
    返回：
        {
            "success": true,
            "data": {
                "report_id": "report_xxxx",
                ...
            }
        }
    """
    try:
        report = ReportManager.get_report_by_simulation(simulation_id)
        
        if not report:
            return jsonify({
                "success": False,
                "error": f"该模拟暂无报告: {simulation_id}",
                "has_report": False
            }), 404
        
        return jsonify({
            "success": True,
            "data": report.to_dict(),
            "has_report": True
        })
        
    except Exception as e:
        logger.error(f"获取报告失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@report_bp.route('/list', methods=['GET'])
def list_reports():
    """
    列出所有报告
    
    Query参数：
        simulation_id: 按模拟ID过滤（可选）
        limit: 返回数量限制（默认50）
    
    返回：
        {
            "success": true,
            "data": [...],
            "count": 10
        }
    """
    try:
        simulation_id = request.args.get('simulation_id')
        limit = request.args.get('limit', 50, type=int)
        
        reports = ReportManager.list_reports(
            simulation_id=simulation_id,
            limit=limit
        )
        
        return jsonify({
            "success": True,
            "data": [r.to_dict() for r in reports],
            "count": len(reports)
        })
        
    except Exception as e:
        logger.error(f"列出报告失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@report_bp.route('/<report_id>/download', methods=['GET'])
def download_report(report_id: str):
    """
    下载报告（Markdown格式）
    
    返回Markdown文件
    """
    try:
        report = ReportManager.get_report(report_id)
        
        if not report:
            return jsonify({
                "success": False,
                "error": f"报告不存在: {report_id}"
            }), 404
        
        md_path = ReportManager._get_report_markdown_path(report_id)
        
        if not os.path.exists(md_path):
            # 如果MD文件不存在，生成一个临时文件
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
                f.write(report.markdown_content)
                temp_path = f.name
            
            return send_file(
                temp_path,
                as_attachment=True,
                download_name=f"{report_id}.md"
            )
        
        return send_file(
            md_path,
            as_attachment=True,
            download_name=f"{report_id}.md"
        )
        
    except Exception as e:
        logger.error(f"下载报告失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


def _md_to_html(md: str) -> str:
    """Convert markdown to HTML (subset: headings, bold, lists, blockquotes, code, hr)."""
    import re, html as htmllib

    lines = md.split('\n')
    out = []
    in_ul = False
    in_code = False

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append('</ul>')
            in_ul = False

    for line in lines:
        # fenced code block toggle
        if line.strip().startswith('```'):
            if in_code:
                out.append('</code></pre>')
                in_code = False
            else:
                close_ul()
                out.append('<pre><code>')
                in_code = True
            continue
        if in_code:
            out.append(htmllib.escape(line))
            continue

        # headings
        m = re.match(r'^(#{1,4})\s+(.*)', line)
        if m:
            close_ul()
            lvl = len(m.group(1)) + 1  # h2-h5
            lvl = min(lvl, 5)
            out.append(f'<h{lvl}>{htmllib.escape(m.group(2))}</h{lvl}>')
            continue

        # hr
        if re.match(r'^[-*_]{3,}\s*$', line):
            close_ul()
            out.append('<hr/>')
            continue

        # blockquote
        if line.startswith('> '):
            close_ul()
            content = line[2:]
            content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
            out.append(f'<blockquote>{content}</blockquote>')
            continue

        # unordered list
        if re.match(r'^[-*] ', line):
            if not in_ul:
                out.append('<ul>')
                in_ul = True
            item = line[2:]
            item = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', item)
            out.append(f'<li>{item}</li>')
            continue

        close_ul()

        # blank line → paragraph break
        if not line.strip():
            out.append('<br/>')
            continue

        # inline: bold, italic
        line = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', line)
        line = re.sub(r'\*(.+?)\*', r'<em>\1</em>', line)
        out.append(f'<p>{line}</p>')

    close_ul()
    if in_code:
        out.append('</code></pre>')

    return '\n'.join(out)


@report_bp.route('/<report_id>/export-html', methods=['GET'])
def export_report_html(report_id: str):
    """
    Export report as a self-contained HTML file for portfolio hosting.
    Includes all styles inline — no backend dependency needed to view.
    """
    try:
        report = ReportManager.get_report(report_id)
        if not report:
            return jsonify({"success": False, "error": f"报告不存在: {report_id}"}), 404

        md_path = ReportManager._get_report_markdown_path(report_id)
        if os.path.exists(md_path):
            with open(md_path, 'r', encoding='utf-8') as f:
                md_content = f.read()
        else:
            md_content = report.markdown_content or ""

        html_body = _md_to_html(md_content)
        title = report.simulation_requirement or report_id
        completed_at = report.completed_at or report.created_at or ""
        demo_url = Config.DEMO_URL

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title[:80]} — MiroFish Report</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0d14;color:#c9d1d9;font-family:'Segoe UI',system-ui,sans-serif;line-height:1.7;padding:0}}
header{{background:#161622;border-bottom:1px solid #30363d;padding:32px 48px;max-width:900px;margin:0 auto}}
header h1{{font-size:13px;text-transform:uppercase;letter-spacing:3px;color:#58a6ff;margin-bottom:12px}}
header .topic{{font-size:22px;font-weight:600;color:#e6edf3;margin-bottom:8px}}
header .meta{{font-size:13px;color:#8b949e;margin-bottom:20px}}
.cta{{display:inline-block;background:#238636;color:#fff;text-decoration:none;padding:10px 20px;border-radius:6px;font-size:14px;font-weight:500}}
.cta:hover{{background:#2ea043}}
main{{max-width:900px;margin:0 auto;padding:40px 48px}}
h2{{font-size:20px;font-weight:600;color:#e6edf3;margin:36px 0 12px;border-bottom:1px solid #21262d;padding-bottom:8px}}
h3{{font-size:17px;font-weight:600;color:#c9d1d9;margin:24px 0 8px}}
h4,h5{{font-size:15px;color:#8b949e;margin:18px 0 6px}}
p{{margin-bottom:12px;color:#c9d1d9}}
ul{{margin:8px 0 16px 24px}}
li{{margin-bottom:4px}}
strong{{color:#e6edf3;font-weight:600}}
em{{color:#a5d6ff;font-style:italic}}
blockquote{{border-left:3px solid #388bfd;padding:8px 16px;margin:16px 0;background:#161b22;color:#8b949e;border-radius:0 4px 4px 0}}
pre{{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:16px;overflow-x:auto;margin:16px 0}}
code{{font-family:'JetBrains Mono','Fira Code',monospace;font-size:13px;color:#e6edf3}}
hr{{border:none;border-top:1px solid #21262d;margin:32px 0}}
br{{display:block;height:6px}}
footer{{max-width:900px;margin:0 auto;padding:24px 48px;border-top:1px solid #21262d;font-size:12px;color:#484f58}}
footer a{{color:#58a6ff;text-decoration:none}}
</style>
</head>
<body>
<header>
  <h1>MiroFish — AI Social Simulation Report</h1>
  <div class="topic">{title}</div>
  <div class="meta">Generated {completed_at}</div>
  <a class="cta" href="{demo_url}">&#9654; Run Your Own Simulation</a>
</header>
<main>
{html_body}
</main>
<footer>
  <p>Built with <a href="https://github.com/fordrainey/MiroFish">MiroFish</a> — AI-powered social simulation &amp; prediction.</p>
</footer>
</body>
</html>"""

        slug = title[:30].replace(' ', '-').lower()
        from flask import Response
        return Response(
            html,
            mimetype='text/html',
            headers={'Content-Disposition': f'attachment; filename="mirofish-{slug}.html"'}
        )

    except Exception as e:
        logger.error(f"导出HTML报告失败: {str(e)}")
        return jsonify({"success": False, "error": str(e), "traceback": traceback.format_exc()}), 500


@report_bp.route('/<report_id>/view', methods=['GET'])
def view_report(report_id: str):
    """
    Serve a shareable, self-contained HTML page for a report.
    Opens in the browser (no Content-Disposition attachment).
    Includes: metadata header, plain-English summary (if cached), full report, collapsible agent log.
    """
    import re, html as htmllib

    try:
        report = ReportManager.get_report(report_id)
        if not report:
            return f"<h1>Report not found</h1><p>No report with ID <code>{report_id}</code> exists.</p>", 404

        report_dir = ReportManager._get_report_folder(report_id)

        # --- Report text ---
        md_path = ReportManager._get_report_markdown_path(report_id)
        if os.path.exists(md_path):
            with open(md_path, 'r', encoding='utf-8') as f:
                md_content = f.read()
        else:
            md_content = report.markdown_content or ""

        # --- Plain English summary (cached) ---
        simplified_path = os.path.join(report_dir, "simplified.md")
        simplified_content = None
        if os.path.exists(simplified_path):
            with open(simplified_path, 'r', encoding='utf-8') as f:
                simplified_content = f.read()

        # --- Agent log ---
        agent_logs = ReportManager.get_agent_log_stream(report_id)
        agent_logs = agent_logs[:500]  # cap to avoid oversized pages

        # --- Render helpers ---
        def _escape(s):
            return htmllib.escape(str(s)) if s else ''

        def _render_log_timeline(logs):
            if not logs:
                return '<p style="color:#999;font-size:13px;">No agent log available.</p>'

            type_colors = {
                'report_start': '#6366f1',
                'planning_start': '#8b5cf6',
                'planning_complete': '#7c3aed',
                'section_start': '#0ea5e9',
                'tool_call': '#f59e0b',
                'tool_result': '#10b981',
                'llm_response': '#6366f1',
                'section_complete': '#22c55e',
                'report_complete': '#16a34a',
                'error': '#ef4444',
            }

            rows = []
            for entry in logs:
                etype = entry.get('type', 'unknown')
                ts = entry.get('timestamp', '')
                if ts and 'T' in ts:
                    ts = ts.split('T')[1][:8]

                # Build a readable summary line
                summary = ''
                if etype == 'tool_call':
                    summary = entry.get('tool_name', '') or entry.get('tool', '')
                    args = entry.get('args', {}) or {}
                    if isinstance(args, dict) and args:
                        first_val = next(iter(args.values()), '')
                        summary += f': {str(first_val)[:80]}'
                elif etype == 'section_start':
                    summary = entry.get('title', '') or entry.get('section_title', '')
                elif etype == 'section_complete':
                    summary = entry.get('title', '') or entry.get('section_title', '')
                elif etype == 'llm_response':
                    content = entry.get('content', '') or entry.get('response', '')
                    summary = str(content)[:120] if content else ''
                elif etype == 'tool_result':
                    result = entry.get('result', '') or entry.get('content', '')
                    summary = str(result)[:120] if result else ''
                else:
                    for key in ('message', 'content', 'description', 'title'):
                        val = entry.get(key)
                        if val:
                            summary = str(val)[:120]
                            break

                color = type_colors.get(etype, '#94a3b8')
                rows.append(
                    f'<div class="log-row">'
                    f'<span class="log-ts">{_escape(ts)}</span>'
                    f'<span class="log-badge" style="background:{color}">{_escape(etype)}</span>'
                    f'<span class="log-summary">{_escape(summary)}</span>'
                    f'</div>'
                )
            return '\n'.join(rows)

        title = report.simulation_requirement or report_id
        completed_at = report.completed_at or report.created_at or ''
        demo_url = Config.DEMO_URL

        report_html = _md_to_html(md_content)
        simplified_html = _md_to_html(simplified_content) if simplified_content else None
        log_html = _render_log_timeline(agent_logs)

        # --- TL;DR section ---
        if simplified_html:
            tldr_section = f'''
<section class="tldr-card">
  <div class="tldr-label">TL;DR — Plain English Summary</div>
  <div class="tldr-body">{simplified_html}</div>
</section>'''
        else:
            tldr_section = '''
<section class="tldr-card tldr-missing">
  <div class="tldr-label">TL;DR — Plain English Summary</div>
  <p class="tldr-hint">Open this report in the MiroFish app and click <strong>Plain English Summary</strong> to generate this section.</p>
</section>'''

        page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_escape(title[:80])} — MiroFish Report</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#fafafa;color:#1a1a1a;font-family:'Space Grotesk','Segoe UI',system-ui,sans-serif;line-height:1.7}}
a{{color:#2563eb;text-decoration:none}}
a:hover{{text-decoration:underline}}

/* Header */
.page-header{{background:#fff;border-bottom:1px solid #e5e7eb;padding:32px 48px 28px}}
.page-header .brand{{font-size:11px;font-weight:700;letter-spacing:3px;color:#6b7280;text-transform:uppercase;margin-bottom:12px}}
.page-header h1{{font-size:26px;font-weight:700;color:#111;line-height:1.3;margin-bottom:10px}}
.page-header .meta{{font-size:13px;color:#6b7280;margin-bottom:20px}}
.cta-btn{{display:inline-block;background:#111;color:#fff;padding:9px 20px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none}}
.cta-btn:hover{{background:#374151;text-decoration:none}}

/* Layout */
.page-body{{max-width:860px;margin:0 auto;padding:40px 48px 80px}}

/* TL;DR card */
.tldr-card{{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:24px 28px;margin-bottom:40px}}
.tldr-missing{{background:#f9fafb;border-color:#e5e7eb}}
.tldr-label{{font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:#16a34a;margin-bottom:12px}}
.tldr-missing .tldr-label{{color:#9ca3af}}
.tldr-body p{{margin-bottom:10px;font-size:15px;color:#1a1a1a}}
.tldr-body h2,.tldr-body h3{{margin:18px 0 8px;color:#111}}
.tldr-body ul{{margin:6px 0 12px 20px}}
.tldr-hint{{font-size:14px;color:#6b7280}}

/* Report content */
.report-content h2{{font-size:20px;font-weight:700;color:#111;margin:36px 0 12px;padding-bottom:8px;border-bottom:1px solid #e5e7eb}}
.report-content h3{{font-size:17px;font-weight:600;color:#374151;margin:24px 0 8px}}
.report-content h4,.report-content h5{{font-size:14px;font-weight:600;color:#6b7280;margin:16px 0 6px}}
.report-content p{{margin-bottom:12px;font-size:15px;color:#374151}}
.report-content ul{{margin:6px 0 14px 22px}}
.report-content li{{margin-bottom:4px;font-size:15px;color:#374151}}
.report-content strong{{color:#111;font-weight:600}}
.report-content em{{color:#4b5563;font-style:italic}}
.report-content blockquote{{border-left:3px solid #93c5fd;padding:8px 16px;margin:16px 0;background:#eff6ff;border-radius:0 4px 4px 0;color:#4b5563}}
.report-content pre{{background:#1e293b;border-radius:6px;padding:16px;overflow-x:auto;margin:16px 0}}
.report-content code{{font-family:'JetBrains Mono','Fira Code',monospace;font-size:13px;color:#e2e8f0}}
.report-content hr{{border:none;border-top:1px solid #e5e7eb;margin:28px 0}}
.report-content br{{display:block;height:4px}}

/* Agent log */
.log-details{{margin-top:48px;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden}}
.log-details summary{{padding:14px 20px;background:#f9fafb;cursor:pointer;font-size:13px;font-weight:600;color:#374151;user-select:none;list-style:none}}
.log-details summary::-webkit-details-marker{{display:none}}
.log-details summary::before{{content:"▶ ";font-size:10px;color:#9ca3af}}
.log-details[open] summary::before{{content:"▼ ";}}
.log-inner{{padding:12px 0;max-height:480px;overflow-y:auto}}
.log-row{{display:flex;align-items:baseline;gap:10px;padding:4px 20px;font-size:12px;line-height:1.5}}
.log-row:hover{{background:#f9fafb}}
.log-ts{{font-family:'JetBrains Mono',monospace;color:#9ca3af;white-space:nowrap;flex-shrink:0;font-size:11px}}
.log-badge{{font-size:10px;font-weight:600;padding:1px 7px;border-radius:10px;color:#fff;white-space:nowrap;flex-shrink:0}}
.log-summary{{color:#374151;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}

/* Footer */
.page-footer{{max-width:860px;margin:0 auto;padding:0 48px 40px;border-top:1px solid #e5e7eb;padding-top:24px;font-size:12px;color:#9ca3af}}
</style>
</head>
<body>
<header class="page-header">
  <div class="brand">MiroFish — AI Social Simulation</div>
  <h1>{_escape(title)}</h1>
  <div class="meta">Generated {_escape(completed_at)}</div>
  <a class="cta-btn" href="{_escape(demo_url)}">&#9654; Run Your Own Simulation</a>
</header>

<div class="page-body">
  {tldr_section}

  <section class="report-content">
    {report_html}
  </section>

  <details class="log-details">
    <summary>Agent Workflow Log ({len(agent_logs)} events)</summary>
    <div class="log-inner">
      {log_html}
    </div>
  </details>
</div>

<footer class="page-footer">
  <p>Built with <a href="https://github.com/fordrainey/MiroFish">MiroFish</a> — AI-powered social simulation &amp; prediction.</p>
</footer>
</body>
</html>"""

        from flask import Response
        return Response(page_html, mimetype='text/html')

    except Exception as e:
        logger.error(f"view_report failed: {str(e)}")
        return f"<h1>Error</h1><pre>{_escape(str(e))}</pre>", 500


@report_bp.route('/<report_id>', methods=['DELETE'])
def delete_report(report_id: str):
    """删除报告"""
    try:
        success = ReportManager.delete_report(report_id)
        
        if not success:
            return jsonify({
                "success": False,
                "error": f"报告不存在: {report_id}"
            }), 404
        
        return jsonify({
            "success": True,
            "message": f"报告已删除: {report_id}"
        })
        
    except Exception as e:
        logger.error(f"删除报告失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== Report Agent对话接口 ==============

@report_bp.route('/chat', methods=['POST'])
def chat_with_report_agent():
    """
    与Report Agent对话
    
    Report Agent可以在对话中自主调用检索工具来回答问题
    
    请求（JSON）：
        {
            "simulation_id": "sim_xxxx",        // 必填，模拟ID
            "message": "请解释一下舆情走向",    // 必填，用户消息
            "chat_history": [                   // 可选，对话历史
                {"role": "user", "content": "..."},
                {"role": "assistant", "content": "..."}
            ]
        }
    
    返回：
        {
            "success": true,
            "data": {
                "response": "Agent回复...",
                "tool_calls": [调用的工具列表],
                "sources": [信息来源]
            }
        }
    """
    try:
        data = request.get_json() or {}
        
        simulation_id = data.get('simulation_id')
        message = data.get('message')
        chat_history = data.get('chat_history', [])
        
        if not simulation_id:
            return jsonify({
                "success": False,
                "error": "请提供 simulation_id"
            }), 400
        
        if not message:
            return jsonify({
                "success": False,
                "error": "请提供 message"
            }), 400
        
        # 获取模拟和项目信息
        manager = SimulationManager()
        state = manager.get_simulation(simulation_id)
        
        if not state:
            return jsonify({
                "success": False,
                "error": f"模拟不存在: {simulation_id}"
            }), 404
        
        project = ProjectManager.get_project(state.project_id)
        if not project:
            return jsonify({
                "success": False,
                "error": f"项目不存在: {state.project_id}"
            }), 404
        
        graph_id = state.graph_id or project.graph_id
        if not graph_id:
            return jsonify({
                "success": False,
                "error": "缺少图谱ID"
            }), 400
        
        simulation_requirement = project.simulation_requirement or ""
        
        # 创建Agent并进行对话
        agent = ReportAgent(
            graph_id=graph_id,
            simulation_id=simulation_id,
            simulation_requirement=simulation_requirement
        )
        
        result = agent.chat(message=message, chat_history=chat_history)
        
        return jsonify({
            "success": True,
            "data": result
        })
        
    except Exception as e:
        logger.error(f"对话失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== 报告进度与分章节接口 ==============

@report_bp.route('/<report_id>/progress', methods=['GET'])
def get_report_progress(report_id: str):
    """
    获取报告生成进度（实时）
    
    返回：
        {
            "success": true,
            "data": {
                "status": "generating",
                "progress": 45,
                "message": "正在生成章节: 关键发现",
                "current_section": "关键发现",
                "completed_sections": ["执行摘要", "模拟背景"],
                "updated_at": "2025-12-09T..."
            }
        }
    """
    try:
        progress = ReportManager.get_progress(report_id)
        
        if not progress:
            return jsonify({
                "success": False,
                "error": f"报告不存在或进度信息不可用: {report_id}"
            }), 404
        
        return jsonify({
            "success": True,
            "data": progress
        })
        
    except Exception as e:
        logger.error(f"获取报告进度失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@report_bp.route('/<report_id>/sections', methods=['GET'])
def get_report_sections(report_id: str):
    """
    获取已生成的章节列表（分章节输出）
    
    前端可以轮询此接口获取已生成的章节内容，无需等待整个报告完成
    
    返回：
        {
            "success": true,
            "data": {
                "report_id": "report_xxxx",
                "sections": [
                    {
                        "filename": "section_01.md",
                        "section_index": 1,
                        "content": "## 执行摘要\\n\\n..."
                    },
                    ...
                ],
                "total_sections": 3,
                "is_complete": false
            }
        }
    """
    try:
        sections = ReportManager.get_generated_sections(report_id)
        
        # 获取报告状态
        report = ReportManager.get_report(report_id)
        is_complete = report is not None and report.status == ReportStatus.COMPLETED
        
        return jsonify({
            "success": True,
            "data": {
                "report_id": report_id,
                "sections": sections,
                "total_sections": len(sections),
                "is_complete": is_complete
            }
        })
        
    except Exception as e:
        logger.error(f"获取章节列表失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@report_bp.route('/<report_id>/section/<int:section_index>', methods=['GET'])
def get_single_section(report_id: str, section_index: int):
    """
    获取单个章节内容
    
    返回：
        {
            "success": true,
            "data": {
                "filename": "section_01.md",
                "content": "## 执行摘要\\n\\n..."
            }
        }
    """
    try:
        section_path = ReportManager._get_section_path(report_id, section_index)
        
        if not os.path.exists(section_path):
            return jsonify({
                "success": False,
                "error": f"章节不存在: section_{section_index:02d}.md"
            }), 404
        
        with open(section_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        return jsonify({
            "success": True,
            "data": {
                "filename": f"section_{section_index:02d}.md",
                "section_index": section_index,
                "content": content
            }
        })
        
    except Exception as e:
        logger.error(f"获取章节内容失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== 报告状态检查接口 ==============

@report_bp.route('/check/<simulation_id>', methods=['GET'])
def check_report_status(simulation_id: str):
    """
    检查模拟是否有报告，以及报告状态
    
    用于前端判断是否解锁Interview功能
    
    返回：
        {
            "success": true,
            "data": {
                "simulation_id": "sim_xxxx",
                "has_report": true,
                "report_status": "completed",
                "report_id": "report_xxxx",
                "interview_unlocked": true
            }
        }
    """
    try:
        report = ReportManager.get_report_by_simulation(simulation_id)
        
        has_report = report is not None
        report_status = report.status.value if report else None
        report_id = report.report_id if report else None
        
        # 只有报告完成后才解锁interview
        interview_unlocked = has_report and report.status == ReportStatus.COMPLETED
        
        return jsonify({
            "success": True,
            "data": {
                "simulation_id": simulation_id,
                "has_report": has_report,
                "report_status": report_status,
                "report_id": report_id,
                "interview_unlocked": interview_unlocked
            }
        })
        
    except Exception as e:
        logger.error(f"检查报告状态失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== Agent 日志接口 ==============

@report_bp.route('/<report_id>/agent-log', methods=['GET'])
def get_agent_log(report_id: str):
    """
    获取 Report Agent 的详细执行日志
    
    实时获取报告生成过程中的每一步动作，包括：
    - 报告开始、规划开始/完成
    - 每个章节的开始、工具调用、LLM响应、完成
    - 报告完成或失败
    
    Query参数：
        from_line: 从第几行开始读取（可选，默认0，用于增量获取）
    
    返回：
        {
            "success": true,
            "data": {
                "logs": [
                    {
                        "timestamp": "2025-12-13T...",
                        "elapsed_seconds": 12.5,
                        "report_id": "report_xxxx",
                        "action": "tool_call",
                        "stage": "generating",
                        "section_title": "执行摘要",
                        "section_index": 1,
                        "details": {
                            "tool_name": "insight_forge",
                            "parameters": {...},
                            ...
                        }
                    },
                    ...
                ],
                "total_lines": 25,
                "from_line": 0,
                "has_more": false
            }
        }
    """
    try:
        from_line = request.args.get('from_line', 0, type=int)
        
        log_data = ReportManager.get_agent_log(report_id, from_line=from_line)
        
        return jsonify({
            "success": True,
            "data": log_data
        })
        
    except Exception as e:
        logger.error(f"获取Agent日志失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@report_bp.route('/<report_id>/agent-log/stream', methods=['GET'])
def stream_agent_log(report_id: str):
    """
    获取完整的 Agent 日志（一次性获取全部）
    
    返回：
        {
            "success": true,
            "data": {
                "logs": [...],
                "count": 25
            }
        }
    """
    try:
        logs = ReportManager.get_agent_log_stream(report_id)
        
        return jsonify({
            "success": True,
            "data": {
                "logs": logs,
                "count": len(logs)
            }
        })
        
    except Exception as e:
        logger.error(f"获取Agent日志失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== 控制台日志接口 ==============

@report_bp.route('/<report_id>/console-log', methods=['GET'])
def get_console_log(report_id: str):
    """
    获取 Report Agent 的控制台输出日志
    
    实时获取报告生成过程中的控制台输出（INFO、WARNING等），
    这与 agent-log 接口返回的结构化 JSON 日志不同，
    是纯文本格式的控制台风格日志。
    
    Query参数：
        from_line: 从第几行开始读取（可选，默认0，用于增量获取）
    
    返回：
        {
            "success": true,
            "data": {
                "logs": [
                    "[19:46:14] INFO: 搜索完成: 找到 15 条相关事实",
                    "[19:46:14] INFO: 图谱搜索: graph_id=xxx, query=...",
                    ...
                ],
                "total_lines": 100,
                "from_line": 0,
                "has_more": false
            }
        }
    """
    try:
        from_line = request.args.get('from_line', 0, type=int)
        
        log_data = ReportManager.get_console_log(report_id, from_line=from_line)
        
        return jsonify({
            "success": True,
            "data": log_data
        })
        
    except Exception as e:
        logger.error(f"获取控制台日志失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@report_bp.route('/<report_id>/console-log/stream', methods=['GET'])
def stream_console_log(report_id: str):
    """
    获取完整的控制台日志（一次性获取全部）
    
    返回：
        {
            "success": true,
            "data": {
                "logs": [...],
                "count": 100
            }
        }
    """
    try:
        logs = ReportManager.get_console_log_stream(report_id)
        
        return jsonify({
            "success": True,
            "data": {
                "logs": logs,
                "count": len(logs)
            }
        })
        
    except Exception as e:
        logger.error(f"获取控制台日志失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== 工具调用接口（供调试使用）==============

@report_bp.route('/tools/search', methods=['POST'])
def search_graph_tool():
    """
    图谱搜索工具接口（供调试使用）
    
    请求（JSON）：
        {
            "graph_id": "mirofish_xxxx",
            "query": "搜索查询",
            "limit": 10
        }
    """
    try:
        data = request.get_json() or {}
        
        graph_id = data.get('graph_id')
        query = data.get('query')
        limit = data.get('limit', 10)
        
        if not graph_id or not query:
            return jsonify({
                "success": False,
                "error": "请提供 graph_id 和 query"
            }), 400
        
        from ..services.zep_tools import ZepToolsService
        
        tools = ZepToolsService()
        result = tools.search_graph(
            graph_id=graph_id,
            query=query,
            limit=limit
        )
        
        return jsonify({
            "success": True,
            "data": result.to_dict()
        })
        
    except Exception as e:
        logger.error(f"图谱搜索失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@report_bp.route('/tools/statistics', methods=['POST'])
def get_graph_statistics_tool():
    """
    图谱统计工具接口（供调试使用）
    
    请求（JSON）：
        {
            "graph_id": "mirofish_xxxx"
        }
    """
    try:
        data = request.get_json() or {}
        
        graph_id = data.get('graph_id')
        
        if not graph_id:
            return jsonify({
                "success": False,
                "error": "请提供 graph_id"
            }), 400
        
        from ..services.zep_tools import ZepToolsService
        
        tools = ZepToolsService()
        result = tools.get_graph_statistics(graph_id)
        
        return jsonify({
            "success": True,
            "data": result
        })
        
    except Exception as e:
        logger.error(f"获取图谱统计失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@report_bp.route('/<report_id>/simplify', methods=['POST'])
def simplify_report(report_id: str):
    """
    Generate a plain-English summary of a completed report using the user's LLM key.

    Request (JSON): {}  — no body required; user keys are pulled from the project.

    Returns:
        { "success": true, "data": { "simplified": "<markdown text>" } }
    """
    try:
        report = ReportManager.get_report(report_id)
        if not report:
            return jsonify({"success": False, "error": f"Report not found: {report_id}"}), 404

        # Get report text — prefer the markdown file, fall back to in-memory content
        md_path = ReportManager._get_report_markdown_path(report_id)
        if os.path.exists(md_path):
            with open(md_path, encoding='utf-8') as f:
                report_text = f.read()
        elif report.markdown_content:
            report_text = report.markdown_content
        else:
            return jsonify({"success": False, "error": "Report text not available yet."}), 400

        if not report_text.strip():
            return jsonify({"success": False, "error": "Report is empty."}), 400

        # Resolve user keys via the project attached to this report's simulation
        sim_state = SimulationManager.get_simulation(report.simulation_id) if report.simulation_id else None
        project = ProjectManager.get_project(sim_state.project_id) if sim_state else None

        _llm_key = None
        _llm_base_url = None
        _llm_model = Config.LLM_MODEL_NAME

        if project:
            _llm_key = project.user_llm_api_key or (None if Config.REQUIRE_USER_KEYS else Config.LLM_API_KEY)
            _llm_base_url = project.user_llm_base_url or (None if Config.REQUIRE_USER_KEYS else Config.LLM_BASE_URL)
            _llm_model = project.user_llm_model_name or Config.LLM_MODEL_NAME
        elif not Config.REQUIRE_USER_KEYS:
            _llm_key = Config.LLM_API_KEY
            _llm_base_url = Config.LLM_BASE_URL

        if not _llm_key:
            return jsonify({"success": False, "error": "user_keys_required",
                            "message": "API key not available. Please re-enter your keys."}), 400

        from ..utils.llm_client import LLMClient
        llm = LLMClient(api_key=_llm_key, base_url=_llm_base_url, model=_llm_model)

        system_prompt = (
            "Rewrite the following simulation report in plain English for a general audience. "
            "Keep all the key findings and predictions but eliminate jargon, shorten sentences, "
            "and lead with the most important conclusion. "
            "The rewritten version should be roughly half the length of the original. "
            "Do not add any information that is not in the original report. "
            "Format the output as clean markdown."
        )

        simplified = llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": report_text}
            ],
            temperature=0.3,
            max_tokens=4096
        )

        # Cache to disk so the /view page can include it without re-running the LLM
        simplified_path = os.path.join(ReportManager._get_report_folder(report_id), "simplified.md")
        try:
            with open(simplified_path, 'w', encoding='utf-8') as f:
                f.write(simplified)
        except Exception:
            pass  # Cache failure is non-fatal

        return jsonify({"success": True, "data": {"simplified": simplified}})

    except Exception as e:
        logger.error(f"simplify_report failed: {str(e)}")
        return jsonify({"success": False, "error": str(e), "traceback": traceback.format_exc()}), 500
