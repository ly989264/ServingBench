#!/bin/bash
# T64: shows or hides the 「有值 / 请求」 column of the two distribution tables, then imports the dashboard again
# (bin/grafana_board.sh; only the dashboard changes, nothing on the nodes or in ClickHouse).
#   bin/has_value.sh show | hide | state
# The setting is the line BOARD_HAS_VALUE= in this folder's deploy.env (1 shows the column; not set or anything else hides
# it), so an update of the pack keeps it.
. "$(dirname "$0")/lib.sh"
ENV_FILE="$PACK_DIR/deploy.env"
[ -f "$ENV_FILE" ] || fail "没有找到 deploy.env"
now=$(raw_env BOARD_HAS_VALUE "")
case ${1:-} in
  show) want=1 ;;
  hide) want=0 ;;
  state) [ "$now" = 1 ] && echo "「有值 / 请求」列：显示" || echo "「有值 / 请求」列：隐藏"; exit 0 ;;
  *) echo "用法：bin/has_value.sh show | hide | state"; exit 2 ;;
esac
if [ "$now" != "$want" ]; then
  tmp="$ENV_FILE.has_value.$$"
  grep -v -E '^BOARD_HAS_VALUE=' "$ENV_FILE" > "$tmp" || true
  echo "BOARD_HAS_VALUE=$want" >> "$tmp"
  chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod "$(stat -f %Lp "$ENV_FILE" 2>/dev/null || echo 600)" "$tmp"
  mv "$tmp" "$ENV_FILE"
fi
"$PACK_DIR/bin/grafana_board.sh" || fail "看板没有导入，看上面的提示；deploy.env 里已经是 BOARD_HAS_VALUE=$want，修好以后再执行一次"
[ "$want" = 1 ] && echo "RESULT: OK（「有值 / 请求」列已显示）" || echo "RESULT: OK（「有值 / 请求」列已隐藏）"
