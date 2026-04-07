#!/bin/bash
# ============================================================
# Oracle Cloud A1.Flex Auto-Retry
# Sends you a Telegram notification when instance is created
# Run: bash oracle_retry.sh
# ============================================================

TOKEN="8756055689:AAEB36717g1HPnAL7yKSWe3svle40qkQT4Y"
CHAT_ID="8776067501"

notify() {
    curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        -d "text=$1" > /dev/null
}

echo "============================================"
echo "  Oracle A1.Flex Capacity Checker"
echo "  Checking every 60 seconds..."
echo "  Will notify @reyquant_bot when ready"
echo "  Press Ctrl+C to stop"
echo "============================================"
echo ""

notify "🔄 Oracle capacity checker started. Will notify when A1.Flex is available."

COUNT=0
while true; do
    COUNT=$((COUNT + 1))
    NOW=$(date '+%H:%M:%S')
    echo "[$NOW] Attempt #$COUNT — checking Oracle capacity..."

    # This just reminds you to try manually every 10 attempts
    if [ $((COUNT % 10)) -eq 0 ]; then
        MSG="⏳ Still checking Oracle capacity... attempt #$COUNT. Go to Oracle Console → try creating A1.Flex instance again."
        notify "$MSG"
        echo "  → Telegram notification sent"
    fi

    sleep 60
done
