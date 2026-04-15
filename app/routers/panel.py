from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from app.services.memory import MemoryService
from app.config import get_settings
import logging

logger = logging.getLogger(__name__)
router = APIRouter()
memory = MemoryService()
settings = get_settings()

# ── Auth simples por token na URL ─────────────────────────────────────────────
def _check_token(token: str):
    if token != settings.app_secret:
        raise HTTPException(status_code=401, detail="Token inválido")

# ── API de dados ──────────────────────────────────────────────────────────────

@router.get("/panel/leads")
async def get_leads(
    token: str = Query(...),
    owner_id: str = Query(""),
    status: str = Query(""),
    channel: str = Query(""),
    search: str = Query(""),
    limit: int = Query(100),
):
    _check_token(token)
    db = memory.db
    query = db.table("customers").select("*").order("last_contact", desc=True).limit(limit)
    if owner_id:
        query = query.eq("owner_id", owner_id)
    if status:
        query = query.eq("lead_status", status)
    if channel:
        query = query.eq("channel", channel)
    result = query.execute()
    leads = result.data or []
    if search:
        s = search.lower()
        leads = [l for l in leads if s in (l.get("name") or "").lower() or s in (l.get("phone") or "") or s in (l.get("summary") or "").lower()]
    return leads


@router.get("/panel/stats")
async def get_stats(token: str = Query(...), owner_id: str = Query("")):
    _check_token(token)
    db = memory.db
    try:
        from datetime import datetime
        today = datetime.utcnow().date().isoformat()

        q = db.table("customers").select("lead_status,channel,lead_score,last_contact,last_sentiment,sentiment_history")
        if owner_id:
            q = q.eq("owner_id", owner_id)
        result = q.execute()
        leads = result.data or []

        total = len(leads)
        today_leads = sum(1 for l in leads if (str(l.get("last_contact") or ""))[:10] == today)
        hot = sum(1 for l in leads if (l.get("lead_score") or 0) >= 70)
        human = sum(1 for l in leads if l.get("lead_status") == "em_atendimento_humano")
        clientes = sum(1 for l in leads if l.get("lead_status") == "cliente")

        channel_counts = {}
        for l in leads:
            c = l.get("channel") or "não identificado"
            channel_counts[c] = channel_counts.get(c, 0) + 1

        channel_stats = sorted(
            [{"canal": k, "total": v, "pct": round(v / total * 100) if total else 0}
             for k, v in channel_counts.items()],
            key=lambda x: x["total"], reverse=True
        )

        # Sentimento agregado
        sentiments = {"positivo": 0, "neutro": 0, "negativo": 0, "frustrado": 0, "entusiasmado": 0}
        for l in leads:
            s = l.get("last_sentiment")
            if s and s in sentiments:
                sentiments[s] += 1
        total_sent = sum(sentiments.values()) or 1
        sentiment_stats = {k: {"total": v, "pct": round(v / total_sent * 100)} for k, v in sentiments.items() if v > 0}

        return {
            "total": total,
            "hoje": today_leads,
            "quentes": hot,
            "em_atendimento": human,
            "clientes": clientes,
            "canais": channel_stats,
            "sentimento": sentiment_stats,
        }
    except Exception as e:
        logger.error(f"[Panel Stats] erro: {e}")
        return {"total": 0, "hoje": 0, "quentes": 0, "em_atendimento": 0, "clientes": 0, "canais": [], "sentimento": {}}


@router.get("/panel/lead/{phone}/messages")
async def get_lead_messages(phone: str, token: str = Query(...), owner_id: str = Query(""), limit: int = 10):
    _check_token(token)
    db = memory.db
    q = db.table("messages").select("role,content,created_at").eq("phone", phone).order("created_at", desc=True).limit(limit)
    if owner_id:
        q = q.eq("owner_id", owner_id)
    result = q.execute()
    msgs = list(reversed(result.data or []))
    return msgs


@router.get("/panel/owners")
async def get_owners(token: str = Query(...)):
    _check_token(token)
    db = memory.db
    result = db.table("owners").select("id,business_name,phone,agent_mode").execute()
    return result.data or []


@router.get("/panel/export")
async def export_leads(token: str = Query(...), owner_id: str = Query("")):
    """Exporta leads como CSV."""
    _check_token(token)
    import csv, io
    db = memory.db
    q = db.table("customers").select("*").order("last_contact", desc=True)
    if owner_id:
        q = q.eq("owner_id", owner_id)
    result = q.execute()
    leads = result.data or []

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "name","phone","channel","lead_score","lead_status",
        "last_intent","total_messages","last_contact","summary"
    ])
    writer.writeheader()
    for l in leads:
        writer.writerow({k: l.get(k, "") for k in writer.fieldnames})

    from fastapi.responses import Response
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"}
    )


# ── Dashboard HTML ────────────────────────────────────────────────────────────

@router.get("/panel", response_class=HTMLResponse)
async def dashboard(token: str = Query(...)):
    _check_token(token)
    html = _build_html(token)
    return HTMLResponse(content=html)


def _build_html(token: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Painel de Leads</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f0f; color: #e0e0e0; min-height: 100vh; }}
  .header {{ background: #1a1a1a; border-bottom: 1px solid #2a2a2a; padding: 16px 24px; display: flex; align-items: center; gap: 12px; }}
  .header h1 {{ font-size: 18px; font-weight: 600; color: #fff; }}
  .header span {{ font-size: 12px; color: #666; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; padding: 20px 24px 0; }}
  .stat {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px; padding: 16px; }}
  .stat .val {{ font-size: 28px; font-weight: 700; color: #fff; }}
  .stat .lbl {{ font-size: 12px; color: #666; margin-top: 4px; }}
  .stat.hot .val {{ color: #ff6b35; }}
  .stat.human .val {{ color: #4fc3f7; }}
  .stat.today .val {{ color: #66bb6a; }}
  .channels {{ padding: 12px 24px 0; display: flex; gap: 8px; flex-wrap: wrap; }}
  .ch-tag {{ background: #1e1e1e; border: 1px solid #2a2a2a; border-radius: 20px; padding: 4px 12px; font-size: 12px; color: #aaa; }}
  .ch-tag span {{ color: #fff; font-weight: 600; }}
  .toolbar {{ padding: 16px 24px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
  .toolbar input {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 8px 14px; color: #e0e0e0; font-size: 14px; width: 240px; outline: none; }}
  .toolbar input:focus {{ border-color: #444; }}
  .toolbar select {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 8px 12px; color: #e0e0e0; font-size: 14px; outline: none; cursor: pointer; }}
  .toolbar a.btn {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 8px 14px; color: #aaa; font-size: 13px; text-decoration: none; margin-left: auto; }}
  .toolbar a.btn:hover {{ color: #fff; border-color: #444; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #151515; color: #666; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .5px; padding: 10px 16px; text-align: left; cursor: pointer; user-select: none; position: sticky; top: 0; }}
  th:hover {{ color: #aaa; }}
  td {{ padding: 12px 16px; border-bottom: 1px solid #1a1a1a; font-size: 13px; vertical-align: middle; }}
  tr:hover td {{ background: #161616; cursor: pointer; }}
  .name {{ color: #fff; font-weight: 500; }}
  .phone a {{ color: #4fc3f7; text-decoration: none; font-size: 12px; }}
  .phone a:hover {{ text-decoration: underline; }}
  .score {{ font-weight: 700; }}
  .score.hot {{ color: #ff6b35; }}
  .score.warm {{ color: #ffb74d; }}
  .score.cold {{ color: #666; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 600; }}
  .badge.novo {{ background: #1e2a1e; color: #66bb6a; }}
  .badge.qualificando {{ background: #1e2030; color: #7986cb; }}
  .badge.em_atendimento_humano {{ background: #1e2a30; color: #4fc3f7; }}
  .badge.cliente {{ background: #2a1e10; color: #ffb74d; }}
  .intent {{ font-size: 11px; color: #666; }}
  .ch {{ font-size: 11px; color: #888; }}
  .sentiment {{ font-size: 11px; font-weight: 600; }}
  .sentiment.positivo {{ color: #66bb6a; }}
  .sentiment.entusiasmado {{ color: #81c784; }}
  .sentiment.neutro {{ color: #888; }}
  .sentiment.negativo {{ color: #ef5350; }}
  .sentiment.frustrado {{ color: #ff7043; }}
  .badge.perdido {{ background: #2a1a1a; color: #ef5350; }}
  .sentiment-bar {{ display: flex; gap: 8px; padding: 12px 24px 0; flex-wrap: wrap; }}
  .sent-chip {{ display: flex; align-items: center; gap: 4px; background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 20px; padding: 4px 12px; font-size: 12px; }}
  .sent-dot {{ width: 8px; height: 8px; border-radius: 50%; }}
  .sent-dot.positivo,.sent-dot.entusiasmado {{ background: #66bb6a; }}
  .sent-dot.neutro {{ background: #888; }}
  .sent-dot.negativo {{ background: #ef5350; }}
  .sent-dot.frustrado {{ background: #ff7043; }}
  .summary {{ font-size: 12px; color: #555; max-width: 280px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .date {{ font-size: 11px; color: #555; }}
  .table-wrap {{ overflow-x: auto; padding: 0 24px 24px; }}
  /* Modal */
  .modal-bg {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75); z-index: 100; align-items: center; justify-content: center; }}
  .modal-bg.open {{ display: flex; }}
  .modal {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px; width: 560px; max-width: 95vw; max-height: 85vh; overflow-y: auto; padding: 24px; }}
  .modal h2 {{ font-size: 16px; color: #fff; margin-bottom: 4px; }}
  .modal .meta {{ font-size: 12px; color: #666; margin-bottom: 16px; }}
  .modal .section {{ margin-bottom: 16px; }}
  .modal .section h3 {{ font-size: 11px; text-transform: uppercase; color: #555; letter-spacing: .5px; margin-bottom: 8px; }}
  .modal .summary-box {{ background: #111; border-radius: 8px; padding: 16px; font-size: 13px; color: #aaa; line-height: 1.7; max-height: 200px; overflow-y: auto; }}
  .modal .summary-box .s-label {{ display: inline-block; background: #1e2a30; color: #4fc3f7; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; margin-bottom: 4px; margin-top: 8px; }}
  .modal .summary-box .s-label:first-child {{ margin-top: 0; }}
  .modal .summary-box .s-text {{ color: #ccc; margin: 4px 0 8px 0; }}
  .modal .lead-cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 16px; }}
  .modal .lead-card {{ background: #111; border: 1px solid #222; border-radius: 8px; padding: 10px 12px; }}
  .modal .lead-card .lc-label {{ font-size: 10px; color: #555; text-transform: uppercase; letter-spacing: .5px; }}
  .modal .lead-card .lc-val {{ font-size: 15px; color: #fff; font-weight: 600; margin-top: 2px; }}
  .msg {{ padding: 8px 12px; border-radius: 8px; margin-bottom: 6px; font-size: 13px; line-height: 1.5; max-width: 88%; }}
  .msg.user {{ background: #1e2a30; color: #b0d4e3; align-self: flex-start; }}
  .msg.assistant {{ background: #1e2a1e; color: #a8d4aa; align-self: flex-end; margin-left: auto; }}
  .msg-wrap {{ display: flex; flex-direction: column; }}
  .msg-time {{ font-size: 10px; color: #444; margin-top: 2px; }}
  .close-btn {{ float: right; background: none; border: none; color: #666; font-size: 20px; cursor: pointer; line-height: 1; }}
  .close-btn:hover {{ color: #fff; }}
  .empty {{ text-align: center; padding: 60px; color: #444; font-size: 14px; }}
</style>
</head>
<body>

<div class="header">
  <h1>Painel de Leads</h1>
  <span id="last-update"></span>
</div>

<div class="stats" id="stats-row">
  <div class="stat"><div class="val" id="s-total">—</div><div class="lbl">Total de leads</div></div>
  <div class="stat today"><div class="val" id="s-hoje">—</div><div class="lbl">Contatos hoje</div></div>
  <div class="stat hot"><div class="val" id="s-hot">—</div><div class="lbl">Leads quentes</div></div>
  <div class="stat human"><div class="val" id="s-human">—</div><div class="lbl">Em atendimento</div></div>
  <div class="stat" style="border-left: 3px solid #ffb74d"><div class="val" id="s-clientes" style="color:#ffb74d">—</div><div class="lbl">Clientes</div></div>
</div>

<div class="channels" id="channels-row"></div>
<div class="sentiment-bar" id="sentiment-row"></div>

<div class="toolbar">
  <input type="text" id="search" placeholder="Buscar por nome ou número..." oninput="filterLeads()">
  <select id="filter-status" onchange="filterLeads()">
    <option value="">Todos os status</option>
    <option value="novo">Novo</option>
    <option value="qualificando">Qualificando</option>
    <option value="em_atendimento_humano">Em atendimento</option>
    <option value="cliente">Cliente</option>
  </select>
  <select id="filter-channel" onchange="filterLeads()">
    <option value="">Todos os canais</option>
    <option value="reels">Reels</option>
    <option value="anuncio">Anúncio</option>
    <option value="youtube">YouTube</option>
    <option value="indicacao">Indicação</option>
    <option value="google">Google</option>
    <option value="direct">Direct</option>
    <option value="stories">Stories</option>
  </select>
  <a class="btn" href="/panel/export?token={token}" download>⬇ Exportar CSV</a>
</div>

<div class="table-wrap">
  <table id="leads-table">
    <thead>
      <tr>
        <th onclick="sortBy('name')">Nome ↕</th>
        <th>Contato</th>
        <th onclick="sortBy('lead_score')">Score ↕</th>
        <th>Status</th>
        <th>Sentimento</th>
        <th>Canal</th>
        <th>Intenção</th>
        <th onclick="sortBy('total_messages')">Msgs ↕</th>
        <th onclick="sortBy('last_contact')">Último contato ↕</th>
        <th>Resumo</th>
      </tr>
    </thead>
    <tbody id="leads-body"></tbody>
  </table>
  <div id="empty-state" class="empty" style="display:none">Nenhum lead encontrado.</div>
</div>

<!-- Modal de perfil -->
<div class="modal-bg" id="modal-bg" onclick="closeModal(event)">
  <div class="modal" id="modal">
    <button class="close-btn" onclick="closeModal()">×</button>
    <h2 id="m-name"></h2>
    <div class="meta" id="m-meta"></div>
    <div class="section">
      <h3>Resumo da conversa</h3>
      <div class="summary-box" id="m-summary">—</div>
    </div>
    <div class="section">
      <h3>Últimas mensagens</h3>
      <div class="msg-wrap" id="m-messages"></div>
    </div>
  </div>
</div>

<script>
const TOKEN = '{token}';
let allLeads = [];
let sortKey = 'last_contact';
let sortAsc = false;

async function loadStats() {{
  const r = await fetch(`/panel/stats?token=${{TOKEN}}`);
  const d = await r.json();
  document.getElementById('s-total').textContent = d.total;
  document.getElementById('s-hoje').textContent = d.hoje;
  document.getElementById('s-hot').textContent = d.quentes;
  document.getElementById('s-human').textContent = d.em_atendimento;
  document.getElementById('s-clientes').textContent = d.clientes || 0;
  const ch = document.getElementById('channels-row');
  ch.innerHTML = d.canais.slice(0,6).map(c =>
    `<div class="ch-tag"><span>${{c.canal}}</span> ${{c.pct}}% (${{c.total}})</div>`
  ).join('');
  // Sentimento
  const sr = document.getElementById('sentiment-row');
  const sent = d.sentimento || {{}};
  const sentLabels = {{positivo:'Positivo',entusiasmado:'Entusiasmado',neutro:'Neutro',negativo:'Negativo',frustrado:'Frustrado'}};
  sr.innerHTML = Object.keys(sent).map(k =>
    `<div class="sent-chip"><span class="sent-dot ${{k}}"></span><span style="color:#aaa">${{sentLabels[k]||k}}</span> <span style="color:#fff;font-weight:600">${{sent[k].pct}}%</span></div>`
  ).join('');
}}

async function loadLeads() {{
  const r = await fetch(`/panel/leads?token=${{TOKEN}}&limit=200`);
  allLeads = await r.json();
  filterLeads();
  document.getElementById('last-update').textContent = 'Atualizado: ' + new Date().toLocaleTimeString('pt-BR');
}}

function filterLeads() {{
  const search = document.getElementById('search').value.toLowerCase();
  const status = document.getElementById('filter-status').value;
  const channel = document.getElementById('filter-channel').value;
  let leads = allLeads.filter(l => {{
    if (search && !((l.name||'').toLowerCase().includes(search) || (l.phone||'').includes(search))) return false;
    if (status && l.lead_status !== status) return false;
    if (channel && l.channel !== channel) return false;
    return true;
  }});
  leads = leads.sort((a, b) => {{
    let va = a[sortKey] || '', vb = b[sortKey] || '';
    if (typeof va === 'number') return sortAsc ? va - vb : vb - va;
    return sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
  }});
  renderLeads(leads);
}}

function sortBy(key) {{
  if (sortKey === key) sortAsc = !sortAsc;
  else {{ sortKey = key; sortAsc = false; }}
  filterLeads();
}}

function scoreClass(s) {{
  if (s >= 70) return 'hot';
  if (s >= 40) return 'warm';
  return 'cold';
}}

function sentimentIcon(s) {{
  const map = {{positivo:'Positivo',entusiasmado:'Entusiasmado',neutro:'Neutro',negativo:'Negativo',frustrado:'Frustrado'}};
  return map[s] || '—';
}}

function fmtDate(d) {{
  if (!d) return '—';
  const dt = new Date(d);
  const today = new Date();
  if (dt.toDateString() === today.toDateString()) return 'Hoje ' + dt.toLocaleTimeString('pt-BR',{{hour:'2-digit',minute:'2-digit'}});
  return dt.toLocaleDateString('pt-BR',{{day:'2-digit',month:'2-digit'}}) + ' ' + dt.toLocaleTimeString('pt-BR',{{hour:'2-digit',minute:'2-digit'}});
}}

function renderLeads(leads) {{
  const tbody = document.getElementById('leads-body');
  const empty = document.getElementById('empty-state');
  if (!leads.length) {{ tbody.innerHTML=''; empty.style.display='block'; return; }}
  empty.style.display = 'none';
  tbody.innerHTML = leads.map(l => `
    <tr onclick="openLead('${{l.phone}}','${{l.owner_id}}')">
      <td class="name">${{l.name || '—'}}</td>
      <td class="phone"><a href="https://wa.me/${{(l.phone||'').replace(/[^0-9]/g,'')}}" target="_blank" onclick="event.stopPropagation()">${{l.phone}}</a></td>
      <td><span class="score ${{scoreClass(l.lead_score||0)}}">${{l.lead_score||0}}</span></td>
      <td><span class="badge ${{l.lead_status||'novo'}}">${{l.lead_status||'novo'}}</span></td>
      <td><span class="sentiment ${{l.last_sentiment||'neutro'}}">${{sentimentIcon(l.last_sentiment)}}</span></td>
      <td class="ch">${{l.channel||'—'}}</td>
      <td class="intent">${{l.last_intent||'—'}}</td>
      <td style="color:#666">${{l.total_messages||0}}</td>
      <td class="date">${{fmtDate(l.last_contact)}}</td>
      <td class="summary">${{l.summary||'—'}}</td>
    </tr>
  `).join('');
}}

function formatSummary(raw) {{
  if (!raw || raw === '—') return '<em style="color:#555">Sem resumo ainda.</em>';
  // Remove markdown noise
  let text = raw.replace(/[*#]/g, '').replace(/\\n- /g, '\\n').trim();
  // Quebra em blocos por "Resumo" ou "Nota"
  const blocks = text.split(/(?=Resumo da Conversa|\\[Nota)/g).filter(b => b.trim());
  if (blocks.length <= 1 && text.length < 400) return `<div class="s-text">${{text}}</div>`;
  // Pega só os últimos 2 blocos pra não ficar enorme
  const recent = blocks.slice(-2);
  return recent.map((block, i) => {{
    const isNote = block.trim().startsWith('[Nota');
    const label = isNote ? 'Nota do dono' : (i === recent.length -1 ? 'Resumo mais recente' : 'Resumo anterior');
    const clean = block.replace(/^Resumo da Conversa\\s*/i, '').trim();
    return `<div class="s-label">${{label}}</div><div class="s-text">${{clean.substring(0, 300)}}${{clean.length > 300 ? '...' : ''}}</div>`;
  }}).join('');
}}

async function openLead(phone, ownerId) {{
  const lead = allLeads.find(l => l.phone === phone);
  if (!lead) return;
  document.getElementById('m-name').textContent = lead.name || phone;
  // Cards com info do lead
  document.getElementById('m-meta').innerHTML = `
    <div class="lead-cards">
      <div class="lead-card"><div class="lc-label">Contato</div><div class="lc-val"><a href="https://wa.me/${{(phone||'').replace(/[^0-9]/g,'')}}" target="_blank" style="color:#4fc3f7;text-decoration:none">${{phone}}</a></div></div>
      <div class="lead-card"><div class="lc-label">Score</div><div class="lc-val" style="color:${{(lead.lead_score||0)>=70?'#ff6b35':(lead.lead_score||0)>=40?'#ffb74d':'#666'}}">${{lead.lead_score||0}}/100</div></div>
      <div class="lead-card"><div class="lc-label">Canal</div><div class="lc-val">${{lead.channel||'desconhecido'}}</div></div>
      <div class="lead-card"><div class="lc-label">Status</div><div class="lc-val"><span class="badge ${{lead.lead_status||'novo'}}">${{lead.lead_status||'novo'}}</span></div></div>
      <div class="lead-card"><div class="lc-label">Mensagens</div><div class="lc-val">${{lead.total_messages||0}}</div></div>
      <div class="lead-card"><div class="lc-label">Último contato</div><div class="lc-val" style="font-size:12px">${{fmtDate(lead.last_contact)}}</div></div>
      <div class="lead-card"><div class="lc-label">Sentimento</div><div class="lc-val sentiment ${{lead.last_sentiment||'neutro'}}">${{sentimentIcon(lead.last_sentiment)}}</div></div>
      <div class="lead-card"><div class="lc-label">Histórico</div><div class="lc-val" style="font-size:11px">${{(lead.sentiment_history||[]).slice(-5).map(s=>`<span class="sentiment ${{s}}" style="margin-right:4px">${{sentimentIcon(s)}}</span>`).join(' → ')||'—'}}</div></div>
    </div>`;
  document.getElementById('m-summary').innerHTML = formatSummary(lead.summary);
  document.getElementById('m-messages').innerHTML = '<em style="color:#444;font-size:12px">Carregando...</em>';
  document.getElementById('modal-bg').classList.add('open');
  const r = await fetch(`/panel/lead/${{encodeURIComponent(phone)}}/messages?token=${{TOKEN}}&owner_id=${{ownerId}}&limit=10`);
  const msgs = await r.json();
  const wrap = document.getElementById('m-messages');
  if (!msgs.length) {{ wrap.innerHTML = '<em style="color:#444;font-size:12px">Sem mensagens registradas.</em>'; return; }}
  wrap.innerHTML = msgs.map(m => `
    <div class="msg ${{m.role}}">${{m.content}}<div class="msg-time">${{fmtDate(m.created_at)}}</div></div>
  `).join('');
}}

function closeModal(e) {{
  if (!e || e.target === document.getElementById('modal-bg')) {{
    document.getElementById('modal-bg').classList.remove('open');
  }}
}}

// Carrega tudo
loadStats();
loadLeads();
// Atualiza a cada 60 segundos
setInterval(() => {{ loadStats(); loadLeads(); }}, 60000);
</script>
</body>
</html>"""
