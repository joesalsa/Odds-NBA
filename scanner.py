import os
import libsql
import requests
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn

# ==========================================
# CONFIGURACIÓN
# ==========================================
TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
INFER_API_KEY = os.environ.get("INFER_API_KEY", "TU_CLAVE_AQUI")

app = FastAPI()

# ==========================================
# CONEXIÓN A TURSO
# ==========================================
def get_db():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)

def init_db():
    con = get_db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS historial (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            matchup TEXT,
            checkpoint TEXT,
            line REAL,
            over REAL,
            under REAL,
            q1_home INTEGER, q1_away INTEGER,
            q2_home INTEGER, q2_away INTEGER,
            q3_home INTEGER, q3_away INTEGER,
            total_pts INTEGER,
            timestamp TEXT,
            UNIQUE(event_id, checkpoint)
        )
    """)
    con.commit()
    con.close()

def guardar_en_db(event_id, matchup, checkpoint, ln, q_h, q_a, total):
    try:
        con = get_db()
        con.execute("""
            INSERT OR IGNORE INTO historial 
            (event_id, matchup, checkpoint, line, over, under, q1_home, q1_away, q2_home, q2_away, q3_home, q3_away, total_pts, timestamp) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event_id, matchup, checkpoint, 
            ln.get("line"), ln.get("over"), ln.get("under"),
            q_h[0] if len(q_h) > 0 else 0, q_a[0] if len(q_a) > 0 else 0,
            q_h[1] if len(q_h) > 1 else 0, q_a[1] if len(q_a) > 1 else 0,
            q_h[2] if len(q_h) > 2 else 0, q_a[2] if len(q_a) > 2 else 0,
            total, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
        con.commit()
        print(f"✅ GUARDADO EN TURSO: {matchup} | {checkpoint} | Línea: {ln.get('line')} | Total: {total}")
    except Exception as e:
        print(f"❌ Error guardando en Turso: {e}")
    finally:
        con.close()

def ya_existe(event_id, checkpoint):
    con = get_db()
    res = con.execute("SELECT id FROM historial WHERE event_id=? AND checkpoint=?", (event_id, checkpoint)).fetchone()
    con.close()
    return res is not None

# ==========================================
# API: ESPN
# ==========================================
def get_espn_games():
    try:
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=15)
        out = []
        for ev in r.json().get("events", []):
            comp = ev.get("competitions", [{}])[0]
            cs = comp.get("competitors", [])
            if len(cs) < 2: continue
            home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
            away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
            
            q_h = [int(q.get("value", 0)) for q in home.get("linescores", [])]
            q_a = [int(q.get("value", 0)) for q in away.get("linescores", [])]
            
            st = ev.get("status", {})
            out.append({
                "id": ev.get("id"),
                "matchup": f"{away.get('team', {}).get('displayName')} vs {home.get('team', {}).get('displayName')}",
                "status": st.get("type", {}).get("description", ""),
                "period": int(st.get("period", 0) or 0),
                "clock": st.get("displayClock", "") or "",
                "q_h": q_h,
                "q_a": q_a,
                "total": sum(q_h) + sum(q_a)
            })
        return out
    except Exception as e:
        print(f"[ESPN Error] {e}")
        return []

# ==========================================
# API: INFERSPORTS
# ==========================================
def get_infer_odds():
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Authorization': f'Bearer {INFER_API_KEY}'
        }
        r = requests.get("https://api.infersports.dev/v1/events/live", headers=headers, timeout=15)
        if r.status_code != 200: return {}
        
        events = [ev for ev in r.json().get("data", []) if ev.get("sport") == "basketball" and ev.get("league", {}).get("name") == "NBA"]
        odds_dict = {}
        
        for ev in events:
            home_short = ev["home_team"]["name"].split(".")[-1].strip().lower().rstrip('s')
            away_short = ev["away_team"]["name"].split(".")[-1].strip().lower().rstrip('s')
            event_key = f"{away_short} vs {home_short}"
            
            r2 = requests.get(f"https://api.infersports.dev/v1/events/{ev['id']}/odds?format=decimal", headers=headers, timeout=15)
            if r2.status_code == 200:
                mejor_balance = 999
                mejor_odd = None
                
                for odd in r2.json().get("odds", []):
                    if odd.get("market_type") == "totals" and odd.get("period") == "full_time" and odd.get("status") == "open":
                        try:
                            over_p = float(odd["prices"].get("over", 0))
                            under_p = float(odd["prices"].get("under", 0))
                        except: continue
                        
                        balance = abs(over_p - 2.0) + abs(under_p - 2.0)
                        if balance < mejor_balance:
                            mejor_balance = balance
                            mejor_odd = {
                                "line": float(odd.get("line")),
                                "over": over_p,
                                "under": under_p
                            }
                
                if mejor_odd:
                    odds_dict[event_key] = mejor_odd
        return odds_dict
    except Exception as e:
        print(f"[Infer Error] {e}")
        return {}

# ==========================================
# LÓGICA DE ESCANEO (AHORRO EXTREMO)
# ==========================================
def scan_games():
    games = get_espn_games()
    if not games: return
    
    partidos_a_guardar = []
    
    # 1. Primero revisamos ESPN (gratis) para ver si ALGUIEN está en HT o Q3
    for g in games:
        checkpoint = None
        if "Half" in g["status"] or (g["period"] == 2 and g["clock"] in ["0.0", "0:00"]):
            checkpoint = "HT"
        elif "End of 3rd" in g["status"] or (g["period"] == 3 and g["clock"] in ["0.0", "0:00"]):
            checkpoint = "Q3_END"
        
        if not checkpoint:
            continue
            
        if ya_existe(g["id"], checkpoint):
            continue
            
        partidos_a_guardar.append((g, checkpoint))
        
    # 2. SI NO HAY PARTIDOS EN HT O Q3, NOS SALIMOS SIN GASTAR CRÉDITOS
    if not partidos_a_guardar:
        return
        
    # 3. Si hay partidos, AHORA SÍ gastamos 1 petición a InferSports
    print(f"🔍 Detectados {len(partidos_a_guardar)} partidos en HT/Q3. Consultando InferSports...")
    infer_odds = get_infer_odds()
    
    for g, checkpoint in partidos_a_guardar:
        team_away = g["matchup"].split(" vs ")[0].split()[-1].lower().rstrip('s')
        team_home = g["matchup"].split(" vs ")[1].split()[-1].lower().rstrip('s')
        
        ln = None
        for key, odds in infer_odds.items():
            if team_away in key and team_home in key:
                ln = odds
                break
        
        if ln:
            guardar_en_db(g["id"], g["matchup"], checkpoint, ln, g["q_h"], g["q_a"], g["total"])
        else:
            print(f"⚠️ {g['matchup']} en {checkpoint}, pero no hay cuota.")

# ==========================================
# RUTA WEB (La página para ver los datos)
# ==========================================
@app.get("/", response_class=HTMLResponse)
def read_root():
    try:
        con = get_db()
        cursor = con.execute("SELECT * FROM historial ORDER BY id DESC")
        rows = cursor.fetchall()
        con.close()
        
        html = """
        <html>
        <head>
            <title>NBA Scanner - Historial</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body { font-family: -apple-system, system-ui; background-color: #0f172a; color: #e2e8f0; padding: 20px; }
                table { width: 100%; border-collapse: collapse; margin-top: 20px; background-color: #1e293b; border-radius: 8px; overflow: hidden; }
                th, td { padding: 12px; border-bottom: 1px solid #334155; text-align: center; font-size: 14px; }
                th { background-color: #1e293b; color: #94a3b8; text-transform: uppercase; font-size: 12px; }
                tr:hover { background-color: #334155; }
                .badge-ht { color: #eab308; font-weight: bold; }
                .badge-q3 { color: #3b82f6; font-weight: bold; }
            </style>
        </head>
        <body>
            <h1>🏀 NBA Scanner - Historial de Líneas</h1>
            <p style="color: #94a3b8;">Estos datos están guardados permanentemente en Turso DB.</p>
        """
        
        if not rows:
            html += "<p style='margin-top:30px; font-size:16px;'>⏳ Aún no hay datos guardados. El bot está esperando a que terminen los cuartos...</p>"
        else:
            columns = ["ID", "Evento", "Partido", "Checkpoint", "Línea", "Over", "Under", "Q1 Loc", "Q1 Vis", "Q2 Loc", "Q2 Vis", "Q3 Loc", "Q3 Vis", "Total Pts", "Fecha"]
            
            html += "<table><tr>"
            for col in columns:
                html += f"<th>{col}</th>"
            html += "</tr>"
            
            for row in rows:
                html += "<tr>"
                for i, item in enumerate(row):
                    if i == 3: # Columna Checkpoint
                        css = "badge-ht" if item == "HT" else "badge-q3"
                        html += f"<td class='{css}'>{item}</td>"
                    else:
                        html += f"<td>{item}</td>"
                html += "</tr>"
            html += "</table>"
            
        html += "</body></html>"
        return html

    except Exception as e:
        return f"<h1>Error al cargar datos</h1><p>{e}</p>"

@app.get("/health")
@app.head("/health")
def health_check():
    return {"status": "ok"}

# ==========================================
# INICIO
# ==========================================
@app.on_event("startup")
def startup_event():
    init_db()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(scan_games, "interval", seconds=60)
    scheduler.start()
    print("🚀 Scanner iniciado (Modo Ahorro). Esperando HT o Fin de Q3...")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("scanner:app", host="0.0.0.0", port=port)
