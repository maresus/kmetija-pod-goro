# Kmetija Pod Goro AI – Digitalni turistični asistent

## 🧪 Smoke testi
```bash
# Zaženi server
uvicorn main:app --reload --port 8000

# V drugem terminalu
./tests/smoke_test.sh
```

## 🔐 Environment spremenljivke

| Spremenljivka | Opis | Obvezno |
|---------------|------|---------|
| OPENAI_API_KEY | OpenAI API ključ | DA |
| DATABASE_URL | PostgreSQL connection string | DA (production) |
| ADMIN_TOKEN | Token za admin API | DA |
| WEBHOOK_SECRET | HMAC secret za WordPress webhook | NE (dev) |
| RESEND_API_KEY | Resend API za email | DA |

## 📡 API Endpoints

### Chat
- POST /chat - Pošlji sporočilo chatbotu

### Admin
- GET /api/admin/reservations - Seznam rezervacij
- PATCH /api/admin/reservations/{id} - Posodobi rezervacijo
- POST /api/admin/reservations/{id}/confirm - Potrdi
- POST /api/admin/reservations/{id}/reject - Zavrni

### Webhook
- POST /api/webhook/reservation - WordPress webhook (HMAC zaščiten)

## 🚀 Deployment

GitHub + Railway flow (kratko):

- Koda je v GitHub repozitoriju.
- Vsak commit na main sproži Railway deploy.
- Railway ima ločene Environment Variables (OPENAI, RESEND, IMAP, DATABASE_URL …).
- Lokalno testiramo v venv, nato pushamo na GitHub.
- Railway avtomatsko povleče main in zgradi novo verzijo.

Kako delamo:

- Lokalno spremembe → test
- git add → git commit → git push
- Railway sam deploya

Kje se spremlja:

- Railway → Deployments → Logs
- Admin panel: /admin
- Chat UI: /

## ✅ Implementirano / Manjka

Implementirano:
- Router V2 (pravila + entitete) brez LLM-ja: `app/services/router_agent.py`
- LLM function-calling za routing rezervacij (`reservation_intent`): `app/services/chat_router.py`
- RAG nad `knowledge.jsonl` + LLM odgovor: `app/rag/knowledge_base.py`, uporaba v `app/services/chat_router.py`
- Turisticni RAG z ChromaDB (okolica): `app/rag/chroma_service.py`
- Dinamicna razpolozljivost iz baze (SQLite/Postgres) v booking flowu: `app/services/reservation_service.py`, `app/services/chat_router.py`
- Pravila/validacija za rezervacije (datumi, dnevi, ure): `app/services/reservation_service.py`, `app/services/chat_router.py`
- Staticni “FAQ” odgovori (brez LLM) za kriticne informacije: `app/services/chat_router.py`
- Tool schema za dinamicne podatke (check_availability) + obvezna uporaba orodja za preverjanje razpolozljivosti: `app/services/chat_router.py`, `app/services/reservation_service.py`

Manjka ali je delno:
- Hybrid retrieval (BM25 + vector) za glavno znanje; trenutno je heuristika token overlap: `app/rag/knowledge_base.py`
- Re-ranker nad rezultati (explicitna faza re-rank): ni prisotno
- Stroga validacija LLM izhodov (npr. “odgovor mora biti podprt z virom”): ni implementirano
- Global confidence gating za LLM odgovore (samo delno pri `semantic_info`): `app/services/chat_router.py`
- Cache za retrieval/odgovore: ni implementirano
