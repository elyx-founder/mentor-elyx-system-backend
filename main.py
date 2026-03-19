from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import os, json, httpx, hashlib, secrets, re
from datetime import datetime, timedelta

app = FastAPI(title="ELYX Backend v2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_hex(32))

LEVEL_NAMES = ["INITIATOR","SEEKER","BUILDER","DOMINATOR","ELITE","APEX"]
THRESHOLDS = [0, 50, 150, 300, 500, 750]

async def sb(method, path, data=None):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    url = f"{SUPABASE_URL}/rest/v1{path}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.request(method, url, json=data, headers=headers)
        if r.status_code in [200, 201]:
            return r.json()
        return None

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def make_token(uid):
    import hmac, base64
    payload = json.dumps({"uid": uid, "exp": (datetime.utcnow() + timedelta(days=90)).isoformat()})
    sig = hmac.new(JWT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.b64encode(f"{payload}.{sig}".encode()).decode()

def verify_token(token):
    try:
        import hmac, base64
        decoded = base64.b64decode(token.encode()).decode()
        payload_str, sig = decoded.rsplit(".", 1)
        expected = hmac.new(JWT_SECRET.encode(), payload_str.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected): return None
        payload = json.loads(payload_str)
        if datetime.fromisoformat(payload["exp"]) < datetime.utcnow(): return None
        return payload["uid"]
    except:
        return None

async def get_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "No token")
    uid = verify_token(authorization.split(" ")[1])
    if not uid: raise HTTPException(401, "Invalid token")
    users = await sb("GET", f"/users?id=eq.{uid}&select=*")
    if not users: raise HTTPException(401, "User not found")
    return users[0]

def calc_level(coins):
    for i in range(len(THRESHOLDS)-1, -1, -1):
        if coins >= THRESHOLDS[i]: return i
    return 0

class SignupReq(BaseModel):
    email: str
    password: str
    name: str
    profession: str
    goal: str
    tone: str = "balanced"

class LoginReq(BaseModel):
    email: str
    password: str

class ChatReq(BaseModel):
    message: str

class ProofReq(BaseModel):
    task_id: str
    proof_text: str = ""
    proof_type: str = "text"

class UpdateReq(BaseModel):
    name: Optional[str] = None
    goal: Optional[str] = None
    profession: Optional[str] = None
    tone: Optional[str] = None

@app.get("/health")
async def health():
    return {"status": "online", "version": "2.0"}

@app.post("/auth/signup")
async def signup(req: SignupReq):
    existing = await sb("GET", f"/users?email=eq.{req.email}&select=id")
    if existing: raise HTTPException(400, "Email exists")
    uid = secrets.token_hex(16)
    user = await sb("POST", "/users", {
        "id": uid, "email": req.email,
        "password_hash": hash_pw(req.password),
        "name": req.name, "profession": req.profession,
        "goal": req.goal, "tone": req.tone,
        "level": 0, "coins": 0, "tasks_done": 0, "streak": 0,
        "last_active": datetime.utcnow().isoformat()
    })
    if not user: raise HTTPException(500, "Signup failed")
    u = user[0] if isinstance(user, list) else user
    return {"token": make_token(uid), "user": u}

@app.post("/auth/login")
async def login(req: LoginReq):
    users = await sb("GET", f"/users?email=eq.{req.email}&password_hash=eq.{hash_pw(req.password)}&select=*")
    if not users: raise HTTPException(401, "Invalid credentials")
    return {"token": make_token(users[0]["id"]), "user": users[0]}

@app.get("/user/me")
async def me(user=Depends(get_user)):
    return user

@app.patch("/user/update")
async def update(req: UpdateReq, user=Depends(get_user)):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    if updates:
        result = await sb("PATCH", f"/users?id=eq.{user['id']}", updates)
        return result[0] if result else user
    return user

@app.post("/chat/message")
async def chat(req: ChatReq, user=Depends(get_user)):
    history = await sb("GET", f"/messages?user_id=eq.{user['id']}&order=created_at.desc&limit=10&select=*") or []
    history.reverse()

    await sb("POST", "/messages", {
        "user_id": user["id"], "role": "user",
        "content": req.message, "created_at": datetime.utcnow().isoformat()
    })

    n = user["name"].split()[0]
    g = user["goal"]
    level = user["level"]
    tone = user.get("tone", "balanced")
    streak = user["streak"]
    coins = user["coins"]

    tone_map = {
        "intense": "ruthless and direct, zero excuses",
        "balanced": "firm but caring",
        "gentle": "warm and encouraging"
    }
    personas = [
        f"dark magnetic mentor — short punchy sentences, {tone_map[tone]}, make {n} feel chosen",
        f"electric hype coach — celebrate wins hard, {tone_map[tone]}",
        f"strategic commander — connect every action to the big vision, {tone_map[tone]}",
        f"wise advisor — ask reframing questions, {tone_map[tone]}",
        f"cold elite peer — minimal words maximum weight, {tone_map[tone]}",
        f"philosophical oracle — connect grind to legacy, {tone_map[tone]}"
    ]

    lang = "Reply in Hinglish." if any(w in req.message.lower() for w in ["hai","hain","kya","nahi","bhai","yaar","kr","ho","tha"]) else "Reply in English."

    past = ""
    if history:
        topics = [m["content"][:40] for m in history[-3:] if m["role"]=="user"]
        if topics: past = f"\nUser recently discussed: {'; '.join(topics)}"

    system = f"""You are ELYX — {n}'s personal AI life coach. Style: {personas[min(level,5)]}

PERSON: {n} | {user['profession']} | Goal: "{g}" | Level: {level} | Coins: {coins} | Streak: {streak}d{past}

RULES:
1. Mention goal "{g}" every message
2. Use name "{n}" naturally  
3. Max 4 sentences. Zero filler.
4. End with ONE question or "Do this now:"
5. Never say you are AI. You are ELYX.
6. {lang}
7. Every word must feel written only for {n}"""

    contents = [{"role": "user" if m["role"]=="user" else "model", "parts":[{"text":m["content"]}]} for m in history]
    contents.append({"role":"user","parts":[{"text":req.message}]})

    reply = f"{n}, what specific step did you take toward '{g}' today?"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
            json={"systemInstruction":{"parts":[{"text":system}]},"contents":contents,"generationConfig":{"maxOutputTokens":400,"temperature":0.9}}
        )
        if r.status_code == 200:
            t = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if t: reply = t

    await sb("POST", "/messages", {"user_id":user["id"],"role":"assistant","content":reply,"created_at":datetime.utcnow().isoformat()})
    await sb("PATCH", f"/users?id=eq.{user['id']}", {"last_active":datetime.utcnow().isoformat()})
    return {"reply": reply}

@app.get("/chat/history")
async def history(user=Depends(get_user)):
    msgs = await sb("GET", f"/messages?user_id=eq.{user['id']}&order=created_at.asc&limit=50&select=*") or []
    return {"messages": msgs}

@app.post("/tasks/generate")
async def gen_tasks(user=Depends(get_user)):
    today = datetime.utcnow().date().isoformat()
    existing = await sb("GET", f"/tasks?user_id=eq.{user['id']}&date=eq.{today}&select=*") or []
    if existing: return {"tasks": existing}

    n = user["name"].split()[0]
    g = user["goal"]
    p = user["profession"]
    recent = await sb("GET", f"/tasks?user_id=eq.{user['id']}&status=eq.done&order=created_at.desc&limit=5&select=title") or []
    done_titles = [t["title"] for t in recent]

    prompt = f'Generate 3 tasks for {n} ({p}). Goal: "{g}". Recent done: {done_titles}. Each MUST directly help achieve "{g}". Different from recent. Very specific. JSON only: [{{"emoji":"e","title":"t","description":"very specific action for {g}","coins":15}},{{"emoji":"e","title":"t","description":"specific","coins":10}},{{"emoji":"e","title":"t","description":"specific","coins":8}}]'

    tasks_data = []
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
            json={"contents":[{"role":"user","parts":[{"text":prompt}]}],"generationConfig":{"maxOutputTokens":500,"temperature":0.7}}
        )
        if r.status_code == 200:
            raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            m = re.search(r'\[[\s\S]*\]', raw.replace("```json","").replace("```",""))
            if m:
                parsed = json.loads(m.group())
                tasks_data = parsed[:3]

    if not tasks_data:
        tasks_data = [
            {"emoji":"🎯","title":"Deep work session","description":f"90min focused on: {g}","coins":15},
            {"emoji":"📊","title":"Review progress","description":f"What moved you closer to {g} this week?","coins":10},
            {"emoji":"💪","title":"One small win","description":f"The smallest possible step toward {g} right now","coins":8}
        ]

    saved = []
    for i, t in enumerate(tasks_data):
        task = await sb("POST", "/tasks", {
            "user_id":user["id"],"date":today,
            "emoji":t.get("emoji","🎯"),"title":t.get("title",f"Mission {i+1}"),
            "description":t.get("description",""),"coins":t.get("coins",[15,10,8][i]),
            "status":"pending","created_at":datetime.utcnow().isoformat()
        })
        if task: saved.append(task[0] if isinstance(task,list) else task)

    return {"tasks": saved}

@app.get("/tasks/today")
async def today_tasks(user=Depends(get_user)):
    today = datetime.utcnow().date().isoformat()
    tasks = await sb("GET", f"/tasks?user_id=eq.{user['id']}&date=eq.{today}&select=*") or []
    return {"tasks": tasks}

@app.post("/tasks/proof")
async def proof(req: ProofReq, user=Depends(get_user)):
    tasks = await sb("GET", f"/tasks?id=eq.{req.task_id}&user_id=eq.{user['id']}&select=*") or []
    if not tasks: raise HTTPException(404, "Task not found")
    task = tasks[0]
    if task["status"] == "done":
        return {"approved":True,"message":"Already done!","coins_earned":0}

    n = user["name"].split()[0]
    coins = task["coins"]
    approved = True
    message = f"{n} — verified! +{coins} EC EARNED."

    if req.proof_text and len(req.proof_text.split()) >= 5:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
                json={"contents":[{"role":"user","parts":[{"text":f"Task: '{task['title']}'. Proof: '{req.proof_text}'. Approve if genuine. JSON: {{\"approved\":true/false,\"message\":\"ELYX reply, mention +{coins} EC if approved\"}}"}]}],"generationConfig":{"maxOutputTokens":200,"temperature":0.7}}
            )
            if r.status_code == 200:
                raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                m = re.search(r'\{[\s\S]*\}', raw.replace("```json","").replace("```",""))
                if m:
                    res = json.loads(m.group())
                    approved = res.get("approved", True)
                    message = res.get("message", message)

    if approved:
        await sb("PATCH", f"/tasks?id=eq.{req.task_id}", {"status":"done","proof":req.proof_text})
        new_coins = user["coins"] + coins
        new_tasks = user["tasks_done"] + 1
        new_level = calc_level(new_coins)
        await sb("PATCH", f"/users?id=eq.{user['id']}", {"coins":new_coins,"tasks_done":new_tasks,"level":new_level})
        return {"approved":True,"message":message,"coins_earned":coins,"new_coins":new_coins,"leveled_up":new_level>user["level"],"new_level":new_level}

    return {"approved":False,"message":message,"coins_earned":0}

@app.get("/leaderboard")
async def lb(user=Depends(get_user)):
    users = await sb("GET", "/users?select=name,coins,level,last_active&order=coins.desc&limit=20") or []
    result = []
    my_rank = 1
    for i, u in enumerate(users):
        is_me = u["name"] == user["name"]
        if is_me: my_rank = i+1
        result.append({"rank":i+1,"name":u["name"],"coins":u["coins"],"level":u["level"],"is_me":is_me})
    return {"leaderboard":result,"my_rank":my_rank}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT",8000)))
