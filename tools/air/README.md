# tools/air — provozni skripty Airu

Zdroj pravdy pro skripty, ktere na Airu bezi z `~/.local/bin`. `a2t-deploy` je po
fast-forwardu sam nakopiruje (viz `install_tool`), takze uprava = commit sem, push, `a2t-deploy`.

- `a2t-deploy` — nasazeni forku: fetch, ff-only na origin/main, instalace techto skriptu,
  restart botu, overeni „Attach bridge live". Unity podle uzivatele (`agents`, `llm-brain`).
- `agent2telegram` — CLI pro `agents` (`cycle.sh`, `mail-push.sh`: `inject`, `notify`). Spousti
  zdrojovy strom s `PYTHONPATH`, takze bezi vzdy to, co nasadil `a2t-deploy`. Neinstaluj A2T
  pres `pip install`: kopie v site-packages se s nasazenim rozjede (deploy na ni varuje).
- `a2t-watch.sh` — denni hlidac (`a2t-watch.timer` u `agents`, ~8:45): rozchod
  upstream/fork/nasazeno a zustatek kreditu ElevenLabs (`EL_THRESHOLD`). Hlasi jen problem.

Unity systemd (`a2t-watch.service`, `.timer`, `a2t-watch-failed.service`) v repu nejsou — ziji
v `~/.config/systemd/user/` na Airu.
