#!/usr/bin/env bash
# a2t-watch — denni hlidac tri bodu A2T. Nikdy nic nemerguje ani nenasazuje, jen hlasi.
# Hlasi JEN pri rozchodu (denni "vsechno sedi" se prestane cist). Kanaly:
#   1. Ludwiguv upstream vs. tag upstream-reviewed na forku -> pribyly commity k vyberu.
#      Tag, ne origin/main: upstream se prebira cherry-pickem, takze hashe se lisi a
#      "obsazeno v main" se z topologie nepozna (a "zreviewovano a vedome nevzato" uz vubec).
#      Po review na Macu posun tag: git tag -f upstream-reviewed <sha> && git push -f origin refs/tags/upstream-reviewed
#   2. fork origin/main vs. nasazeny kod u llm-brain (z /var/tmp/a2t-head-llm-brain,
#      ktery zapisuje ExecStartPost jeho unity pri kazdem startu — rika, co BEZI)
#   3. fork origin/main vs. nasazeny kod u agents (lokalni HEAD)
# Kdyz cokoli selze, unit skonci nenulove a OnFailure posle alarm pres grain bota.
set -euo pipefail

SRC="$HOME/.agent2telegram-src"
export PYTHONPATH="$SRC"
NOTIFY_CONFIG="$HOME/.config/agent2telegram/grain-mesh-sync.json"

cd "$SRC"
git fetch -q origin
git fetch -q upstream
# Tag ukazuje na upstream commit, ktery neni v historii origin/main — auto-following ho nestahne.
git fetch -qf origin "+refs/tags/upstream-reviewed:refs/tags/upstream-reviewed" || true

FORK="$(git rev-parse origin/main)"
UP="$(git rev-parse upstream/main)"
AGENTS_HEAD="$(git rev-parse HEAD)"
LB_FILE="/var/tmp/a2t-head-llm-brain"
LB_HEAD="$(cat "$LB_FILE" 2>/dev/null || echo MISSING)"

MSG=""

if [ "$LB_HEAD" = "MISSING" ]; then
  MSG+="⚠️ Nejde zjistit, co bezi u llm-brain: $LB_FILE neexistuje (zapisuje ho ExecStartPost agent2telegram.service pri startu). Ticho neni klid."$'\n\n'
fi

if git rev-parse -q --verify refs/tags/upstream-reviewed >/dev/null; then
  N_UP="$(git rev-list --count upstream-reviewed..upstream/main)"
  if [ "$N_UP" -gt 0 ]; then
    MSG+="🔔 Upstream ma $N_UP nezreviewovanych commitu. Review na Macu: git -C ~/ClaudeProjects/Agent2Telegram log --oneline upstream-reviewed..upstream/main; pak posunout tag upstream-reviewed."$'\n'
    MSG+="$(git log --oneline -5 upstream-reviewed..upstream/main)"$'\n\n'
  fi
else
  MSG+="⚠️ Tag upstream-reviewed na forku chybi — nejde poznat, co z upstreamu uz proslo review. Zaloz ho na Macu."$'\n\n'
fi

if [ "$LB_HEAD" != "MISSING" ] && [ "$LB_HEAD" != "$FORK" ]; then
  MSG+="⚠️ llm-brain neni na fork main: nasazeno ${LB_HEAD:0:7}, fork ${FORK:0:7}. Nasadit: ssh llm-brain@air-claw a2t-deploy"$'\n\n'
fi

if [ "$AGENTS_HEAD" != "$FORK" ]; then
  MSG+="⚠️ agents neni na fork main: nasazeno ${AGENTS_HEAD:0:7}, fork ${FORK:0:7}. Nasadit: a2t-deploy"$'\n\n'
fi

# --- ElevenLabs kredity (klic ma User: Access od 31. 8.). Hlasi jen pod prahem. ---
EL_THRESHOLD=4000
EL_KEY="$(grep -oP "(?<=^ELEVENLABS_API_KEY=).*" /etc/agent2telegram/elevenlabs.env 2>/dev/null || true)"
if [ -n "$EL_KEY" ]; then
  EL_JSON="$(curl -s -m 15 -H "xi-api-key: $EL_KEY" https://api.elevenlabs.io/v1/user/subscription || true)"
  EL_LEFT="$(printf '%s' "$EL_JSON" | /usr/bin/python3 -c "import json,sys
try:
    d = json.load(sys.stdin)
    print(d[\"character_limit\"] - d[\"character_count\"])
except Exception:
    print(\"ERR\")" )"
  if [ "$EL_LEFT" = "ERR" ]; then
    MSG+="⚠️ ElevenLabs: nejde precist stav kreditu (API nevratilo platnou odpoved). Ticho neni klid."$'\n\n'
  elif [ "$EL_LEFT" -lt "$EL_THRESHOLD" ]; then
    MSG+="🔋 ElevenLabs: zbyva jen $EL_LEFT kreditu (prah $EL_THRESHOLD). Hlasovky brzy prestanou mluvit."$'\n\n'
  fi
fi

[ -z "$MSG" ] && exit 0

AGENT2TELEGRAM_CONFIG="$NOTIFY_CONFIG" /usr/bin/python3 -m agent2telegram notify "A2T hlidac:
$MSG"
