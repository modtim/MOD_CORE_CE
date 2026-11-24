#!/bin/sh
# MOD install detector (REPORT ONLY) - F15-WORKER-INSTALL-FIND-OLD-MOD-REPORT-ONLY
#
# Prints, in plain words, what OTHER MOD installs already exist on this host:
# containers, compose projects, systemd units, MOD-looking directories and
# leftover model servers. It NEVER stops, disables, removes or changes anything,
# and it never prompts. Leg 1 of WORKER.INSTALL-CLEAN-START-OLD-MOD-DETECT.
#
# Usage:
#   detect-old-mod.sh [--json] [--exclude-unit NAME] [--manager-stacks FILE]
#
#   --json              machine-readable output (valid JSON, one object)
#   --exclude-unit NAME a systemd unit name to skip (the NEW worker unit)
#   --manager-stacks F  file of stack/compose project names THIS manager owns
#                       (one per line); those are labelled OURS and left out of
#                       the foreign list. With no file every MOD-looking item
#                       is `unknown (not stopped)`.
#
# Runs fine with no docker, no systemd, as non-root, and in a few seconds.
set -u

JSON=0
EXCLUDE_UNIT=""
STACKS_FILE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --json) JSON=1 ;;
        --exclude-unit) EXCLUDE_UNIT="${2:-}"; shift ;;
        --manager-stacks) STACKS_FILE="${2:-}"; shift ;;
        -h|--help) echo "usage: detect-old-mod.sh [--json] [--exclude-unit NAME] [--manager-stacks FILE]"; exit 0 ;;
    esac
    shift
done

# Names this manager owns (leg 2 fills this from the manager API; here a file)
STACKS=""
if [ -n "$STACKS_FILE" ] && [ -f "$STACKS_FILE" ]; then
    STACKS=$(tr '\n' ' ' < "$STACKS_FILE" 2>/dev/null)
fi

owns() {
    _n=$1
    for _s in $STACKS; do
        case "$_n" in
            "$_s"|"$_s"*|"*"$_s*) return 0 ;;
        esac
    done
    return 1
}

mod_name() {
    case "$1" in
        my_*|mod_*|modstack*|mod-*) return 0 ;;
        *) return 1 ;;
    esac
}

model_name() {
    case "$1" in
        strata*|*strata*|llama-server*|*llama-server*|vllm*|*vllm*|ollama*|*ollama*) return 0 ;;
        *) return 1 ;;
    esac
}

esc() {
    printf '%s' "$1" | tr -d '\n' | sed 's/\\/\\\\/g; s/"/\\"/g'
}

# ---- containers -------------------------------------------------------------
DOCKER_OK=0
CONT_LINES=""
COMPOSE_LINES=""
MODEL_LINES=""
FOREIGN_COUNT=0
OURS_COUNT=0

if command -v docker >/dev/null 2>&1 && docker ps -a --format '{{.Names}}' >/dev/null 2>&1; then
    DOCKER_OK=1
    NAMES=$(docker ps -a --format '{{.Names}}|{{.State}}' 2>/dev/null) || NAMES=""
    if [ -n "$NAMES" ]; then
        ALLN=$(printf '%s\n' "$NAMES" | cut -d'|' -f1 | tr '\n' ' ')
        # one inspect call for restart policy + compose project label
        # docker inspect -f '{{.Name}}' prints the name WITH a leading slash
        # (/my_fastapi_app); strip it so the parse below matches the names
        # `docker ps --format '{{.Names}}'` gave us.
        INFO=$(docker inspect -f '{{.Name}} {{.HostConfig.RestartPolicy.Name}} {{with .Config.Labels}}{{index . "com.docker.compose.project"}}{{end}}' $ALLN 2>/dev/null | sed 's|^/||')
        printf '%s\n' "$NAMES" | while IFS='|' read -r n st; do
            [ -n "$n" ] || continue
            pol=$(printf '%s\n' "$INFO" | grep -e "^$n " | cut -d' ' -f2)
            [ -n "$pol" ] || pol="unknown"
            proj=$(printf '%s\n' "$INFO" | grep -e "^$n " | cut -d' ' -f3)
            if model_name "$n"; then
                printf 'MODEL|%s|%s|%s\n' "$n" "$st" "$pol"
            fi
            if [ -n "$proj" ] && [ "$proj" != "null" ]; then
                printf 'COMPOSE|%s|%s\n' "$proj" "$n"
            fi
            if mod_name "$n"; then
                if owns "$n"; then
                    printf 'OURS|%s|%s|%s\n' "$n" "$st" "$pol"
                else
                    printf 'FOREIGN|%s|%s|%s\n' "$n" "$st" "$pol"
                fi
            fi
        done > /tmp/detect-old-mod.$$
        CONT_LINES=$(grep -e '^FOREIGN|' /tmp/detect-old-mod.$$ 2>/dev/null)
        OURSLINES=$(grep -e '^OURS|' /tmp/detect-old-mod.$$ 2>/dev/null)
        MODEL_LINES=$(grep -e '^MODEL|' /tmp/detect-old-mod.$$ 2>/dev/null)
        COMPOSE_LINES=$(grep -e '^COMPOSE|' /tmp/detect-old-mod.$$ 2>/dev/null | cut -d'|' -f2 | sort -u)
        rm -f /tmp/detect-old-mod.$$
    fi
fi

# ---- processes (leftover model servers) ------------------------------------
PROC_LINES=""
if command -v ps >/dev/null 2>&1; then
    PROC_LINES=$(ps -eo comm= 2>/dev/null | grep -i -e 'strata\|llama-server\|vllm\|ollama' | sort -u | sed 's/^/PROC|/')
fi

# ---- systemd units ----------------------------------------------------------
UNIT_LINES=""
if command -v systemctl >/dev/null 2>&1; then
    UNIT_LINES=$(systemctl list-units --all --type=service --type=timer --no-legend --no-pager --plain 2>/dev/null \
        | awk -v ex="$EXCLUDE_UNIT" '{st=""; for(i=2;i<=NF;i++){if($i=="active"||$i=="inactive"||$i=="failed"||$i=="starting"||$i=="stopping"||$i=="indeterminate"){st=$i;break}} if($1 ~ /^mod-/ && st!="" && $1!=ex && $1!=ex".service" && $1!=ex".timer") print $1" "st}' \
        | sed 's/ /|/')
fi

# ---- directories ------------------------------------------------------------
DIR_LINES=""
for base in "$HOME" /opt /var/lib/mod; do
    [ -d "$base" ] || continue
    for d in "$base"/mod* "$base"/MOD* "$base"/*; do
        [ -d "$d" ] || continue
        case "$(basename "$d")" in mod*|MOD*) ;; *)
            # everything under /var/lib/mod counts as MOD-looking
            case "$d" in /var/lib/mod/*) ;; *) continue ;; esac ;;
        esac
        sz=$(du -sk "$d" 2>/dev/null | cut -f1)
        [ -n "$sz" ] || sz="?"
        if owns "$(basename "$d")"; then
            printf 'DIR|%s|%s|OURS\n' "$d" "$sz"
        else
            printf 'DIR|%s|%s|FOREIGN\n' "$d" "$sz"
        fi
    done
done 2>/dev/null | sort -u > /tmp/detect-old-mod-dirs.$$
DIR_LINES=$(cat /tmp/detect-old-mod-dirs.$$ 2>/dev/null)
rm -f /tmp/detect-old-mod-dirs.$$

# ---- report -----------------------------------------------------------------
if [ "$JSON" = "1" ]; then
    printf '{"docker_available":%s,"foreign_containers":[' "$([ "$DOCKER_OK" = 1 ] && echo true || echo false)"
    first=1
    printf '%s\n' "$CONT_LINES" | while IFS='|' read -r _ n st pol; do
        [ -n "$n" ] || continue
        [ "$first" = 1 ] || printf ','
        first=0
        printf '{"name":"%s","state":"%s","restart_policy":"%s","kind":"unknown (not stopped)"}' "$(esc "$n")" "$(esc "$st")" "$(esc "$pol")"
    done
    printf '],"compose_projects":['
    first=1
    printf '%s\n' "$COMPOSE_LINES" | while read -r p; do
        [ -n "$p" ] || continue
        [ "$first" = 1 ] || printf ','
        first=0
        printf '"%s"' "$(esc "$p")"
    done
    printf '],"units":['
    first=1
    printf '%s\n' "$UNIT_LINES" | while IFS='|' read -r n st; do
        [ -n "$n" ] || continue
        [ "$first" = 1 ] || printf ','
        first=0
        printf '{"name":"%s","state":"%s"}' "$(esc "$n")" "$(esc "$st")"
    done
    printf '],"dirs":['
    first=1
    printf '%s\n' "$DIR_LINES" | while IFS='|' read -r _ d sz kind; do
        [ -n "$d" ] || continue
        [ "$first" = 1 ] || printf ','
        first=0
        printf '{"path":"%s","size_kb":"%s","kind":"%s"}' "$(esc "$d")" "$sz" "$kind"
    done
    printf '],"model_servers":['
    first=1
    printf '%s\n%s\n' "$MODEL_LINES" "$PROC_LINES" | while IFS='|' read -r _ n rest; do
        [ -n "$n" ] || continue
        [ "$first" = 1 ] || printf ','
        first=0
        printf '"%s"' "$(esc "$n")"
    done
    printf '],"manager_stack_list":"%s"}\n' "$(printf '%s' "$STACKS" | sed 's/ /,/g' | sed 's/^,*//; s/,*$//')"
    exit 0
fi

echo "MOD installs already on this host - REPORT ONLY (nothing was stopped, disabled, removed or changed)"
echo ""
echo "1) Other MOD containers:"
if [ -n "$CONT_LINES" ]; then
    printf '%s\n' "$CONT_LINES" | while IFS='|' read -r _ n st pol; do
        printf '   %s  state=%s  restart=%s  -> unknown (not stopped)\n' "$n" "$st" "$pol"
    done
else
    [ "$DOCKER_OK" = 1 ] && echo "   (none found)" || echo "   docker not available"
fi
echo ""
echo "2) Compose projects:"
[ -n "$COMPOSE_LINES" ] && printf '%s\n' "$COMPOSE_LINES" | sed 's/^/   /' || echo "   (none found)"
echo ""
echo "3) systemd units named mod-*:"
if [ -n "$UNIT_LINES" ]; then
    printf '%s\n' "$UNIT_LINES" | tr '|' ' ' | sed 's/^/   /'
elif ! command -v systemctl >/dev/null 2>&1; then
    echo "   (no systemctl on this host)"
else
    echo "   (none found)"
fi
echo ""
echo "   NOTE: boot jobs such as mod-boot-reconcile.service and mod-boot-reconcile-watchdog.timer"
echo "   plus restart policies other than 'no' can bring an old stack back after a reboot."
echo ""
echo "4) MOD-looking directories (names and sizes only):"
[ -n "$DIR_LINES" ] && printf '%s\n' "$DIR_LINES" | cut -d'|' -f2,3,4 | tr '|' ' ' | sed 's/ / (/; s/ / kb -> /; s/^/   /' || echo "   (none found)"
echo ""
echo "5) Leftover model servers (names only):"
if [ -n "$MODEL_LINES$PROC_LINES" ]; then
    printf '%s\n%s\n' "$MODEL_LINES" "$PROC_LINES" | cut -d'|' -f2 | grep -v '^$' | sed 's/^/   /'
else
    echo "   (none found)"
fi
echo ""
if [ -n "$STACKS" ]; then
    echo "Stacks this manager owns (labelled OURS, left out of the list above): $(printf '%s' "$STACKS" | tr '\n' ' ')"
else
    echo "No manager stack list was given, so every MOD-looking item above is unknown (not stopped)."
fi
exit 0
