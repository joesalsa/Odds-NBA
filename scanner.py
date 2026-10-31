import os
import libsql
import requests
from datetime import datetime
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn

# ==========================================
# CONFIGURACIÓN (Variables de entorno)
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
# LÓGICA DE ESCANEO
# ==========================================
def scan_games():
    games = get_espn_games()
    if not games: return
    
    infer_odds = get_infer_odds()
    
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
# FASTAPI Y SCHEDULER
# ==========================================
@app.on_event("startup")
def startup_event():
    init_db()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(scan_games, "interval", seconds=60)
    scheduler.start()
    print("🚀 Scanner iniciado y programado cada 60 segundos.")

@app.get("/health")
@app.head("/health")
def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("scanner:app", host="0.0.0.0", port=port)
