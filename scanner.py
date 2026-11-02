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
            q4_home INTEGER, q4_away INTEGER,
            total_pts INTEGER,
            timestamp TEXT,
            UNIQUE(event_id, checkpoint)
        )
    """)
    # Agregar columnas Q4 si la tabla ya existía de antes
    try: con.execute("ALTER TABLE historial ADD COLUMN q4_home INTEGER")
    except: pass
    try: con.execute("ALTER TABLE historial ADD COLUMN q4_away INTEGER")
    except: pass
    
    con.commit()
    con.close()

def guardar_en_db(event_id, matchup, checkpoint, ln, q_h, q_a, total):
    try:
        con = get_db()
        con.execute("""
            INSERT OR IGNORE INTO historial 
            (event_id, matchup, checkpoint, line, over, under, q1_home, q1_away, q2_home, q2_away, q3_home, q3_away, q4_home, q4_away, total_pts, timestamp) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event_id, matchup, checkpoint, 
            ln.get("line"), ln.get("over"), ln.get("under"),
            q_h[0] if len(q_h) > 0 else 0, q_a[0] if len(q_a) > 0 else 0,
            q_h[1] if len(q_h) > 1 else 0, q_a[1] if len(q_a) > 1 else 0,
            q_h[2] if len(q_h) > 2 else 0, q_a[2] if len(q_a) > 2 else 0,
            q_h[3] if len(q_h) > 3 else 0, q_a[3] if len(q_a) > 3 else 0,
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
                "state": st.get("type", {}).get("state", ""),
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
# LÓGICA DE ESCANEO (HT, Q3_END y FINAL)
# ==========================================
def scan_games():
    games = get_espn_games()
    if not games: return
    
    partidos_a_guardar = []
    
    for g in games:
        checkpoint = None
        
        # 1. Detectar si el partido YA TERMINÓ
        if g["state"] == "post" or "Final" in g["status"]:
            checkpoint = "FINAL"
        else:
            q2_pts = (g["q_h"][1] if len(g["q_h"]) > 1 else 0) + (g["q_a"][1] if len(g["q_a"]) > 1 else 0)
            q3_pts = (g["q_h"][2] if len(g["q_h"]) > 2 else 0) + (g["q_a"][2] if len(g["q_a"]) > 2 else 0)
            
            if "Half" in g["status"]:
                checkpoint = "HT"
            elif g["period"] == 2 and g["clock"] in ["0.0", "0:00"] and q2_pts > 20:
                checkpoint = "HT"
            elif "End of 3rd" in g["status"]:
                checkpoint = "Q3_END"
            elif g["period"] == 3 and g["clock"] in ["0.0", "0:00"] and q3_pts > 20:
                checkpoint = "Q3_END"
        
        if not checkpoint:
            continue
            
        if ya_existe(g["id"], checkpoint):
            continue
            
        partidos_a_guardar.append((g, checkpoint))
        
    if not partidos_a_guardar:
        return
        
    print(f"🔍 Detectados {len(partidos_a_guardar)} partidos. Consultando InferSports...")
    infer_odds = get_infer_odds()
    
    for g, checkpoint in partidos_a_guardar:
        # Si el partido es FINAL, no necesitamos cuota porque ya terminó
        if checkpoint == "FINAL":
            ln = {"line": None, "over": None, "under": None}
        else:
            team_away = g["matchup"].split(" vs ")[0].split()[-1].lower().rstrip('s')
            team_home = g["matchup"].split(" vs ")[1].split()[-1].lower().rstrip('s')
            
            ln = None
            for key, odds in infer_odds.items():
                if team_away in key and team_home in key:
                    ln = odds
                    break
            
            if not ln:
                print(f"⚠️ {g['matchup']} en {checkpoint}, pero no hay cuota.")
                continue
        
        guardar_en_db(g["id"], g["matchup"], checkpoint, ln, g["q_h"], g["q_a"], g["total"])

# ==========================================
# RUTA WEB (Diseño Claro con Q4)
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
                body { font-family: -apple-system, system-ui, sans-serif; background-color: #f4f6f9; color: #334155; padding: 20px; }
                h1 { color: #1e293b; }
                table { width: 100%; border-collapse: collapse; margin-top: 20px; background-color: #ffffff; border-radius: 10px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); }
                th, td { padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: center; font-size: 14px; }
                th { background-color: #f8fafc; color: #64748b; text-transform: uppercase; font-size: 12px; font-weight: 600; }
                tr:hover { background-color: #f1f5f9; }
                .badge-ht { color: #ca8a04; background: #fef08a; padding: 4px 8px; border-radius: 4px; font-weight: bold; }
                .badge-q3 { color: #2563eb; background: #dbeafe; padding: 4px 8px; border-radius: 4px; font-weight: bold; }
                .badge-final { color: #16a34a; background: #dcfce7; padding: 4px 8px; border-radius: 4px; font-weight: bold; }
            </style>
        </head>
        <body>
            <h1>🏀 NBA Scanner - Historial de Líneas</h1>
            <p style="color: #64748b;">Datos guardados en Turso DB (HT, Q3_END y FINAL).</p>
        """
        
        if not rows:
            html += "<p style='margin-top:30px; font-size:16px;'>⏳ Aún no hay datos guardados. El bot está esperando a que terminen los cuartos...</p>"
        else:
            # Columnas actualizadas con Q4
            columns = ["ID", "Evento", "Partido", "Checkpoint", "Línea", "Over", "Under", "Q1 Loc", "Q1 Vis", "Q2 Loc", "Q2 Vis", "Q3 Loc", "Q3 Vis", "Q4 Loc", "Q4 Vis", "Total Pts", "Fecha"]
            
            html += "<table><tr>"
            for col in columns:
                html += f"<th>{col}</th>"
            html += "</tr>"
            
            for row in rows:
                html += "<tr>"
                for i, item in enumerate(row):
                    if i == 3: # Columna Checkpoint
                        if item == "HT": css = "badge-ht"
                        elif item == "Q3_END": css = "badge-q3"
                        else: css = "badge-final"
                        html += f"<td><span class='{css}'>{item}</span></td>"
                    else:
                        html += f"<td>{item if item is not None else '-'}</td>"
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
    print("🚀 Scanner iniciado. Guardando HT, Q3_END y FINAL...")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("scanner:app", host="0.0.0.0", port=port)
