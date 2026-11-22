# t53drive: what the kits and the glm5.3 site must look like. Sourced by t53drive.sh.
# Every value here was read on the glm5.3 site or from the frozen kits; nothing cluster-specific (instance counts,
# DP, ports) is here: those come from the machine list.

# kits (full sha256)
EXP_KIT_PATCH=4f59ea877db09e46c0c413ccc0fd56b8cc9bf77aab7f7b179536df2fb6d34ef7      # t58-glm53-patch-kit.tar
EXP_KIT_MC=e3e338d5cd5a95d462752286f9f665932e7951f0c1c000f4730bbe9d4aea89a9         # t58-glm53-mooncake-kit.tar
EXP_KIT_PROXY=c08056fed8d83f393459f91f2441dc8dbc6efb8706b358702b01d3c798c05a1f      # t51-glm53-proxy-kit.tar.gz
EXP_MC_EXC_SUMS=a5bb986cd8656b1f57c8de14938ff7497baf515d590550a6e8d21bc231eb4425    # t53-mc-exception/SHA256SUMS
EXP_PANEL_VERSION=v3.4-extra

# the site's original files (stage ①) and the replacement Mooncake library (stage ④)
EXP_CONN=2c4acb3eeb1ced7d                     # mooncake_connector.py, first 16 hex of sha256
EXP_SITE_KV=d7c764bb425993c0                  # vllm_ascend/patch/platform/patch_kv_load_failure_reason.py
EXP_SITE_PREFILL=c36392a6338bc2f1             # vllm_ascend/patch/platform/patch_prefill_length_validation.py
EXP_P2D_ALT="C25"                             # changes that use an alternative anchor
EXP_P2D_EXTRA="p2d_extra X1=1 X2=1/1 X3=2 X4=4/1 X5=1 X6=ok"
EXP_MC_VERDICT_PRE=not_replaced
EXP_MC_ORIG16=3bf82f8b0c5ab715                # libmooncake_store.so on site, size 17610953
MC_NEW_SHA=37a3aaf8acb53c6a3cdb9251b9e6d3e1edc0c13789c8d4138d111b1d71cc4a6f   # lib/libmooncake_store.so of the Mooncake kit
EXP_MC_NEW16=37a3aaf8acb53c6a                 # replacement, size 15402392

# the .comment exception: what glm53-test showed on 11-20 (stage ①, MOONCAKE=replace and STOP_build_differs)
EXP_MC_COMMENT="size=17610953 sha16=3bf82f8b0c5ab715 comment_bytes=51 flags=0x30|mc_comment s1 len=50 text=GCC: (GNU) 12.3.1 (openEuler 12.3.1-110.oe2403sp4) lab=|names=same same=9906 differ=0"

# startup lines and run mode
EXP_P_TP=16
EXP_D_STAGES=forward,sampling,prepare_inputs,lm_head,draft,bookkeeping
MIN_FREE_GB=50                                # node folder's disk (NODE_MIN_DISK_GB of the panel pack)

# the panel two the add writes
NODE_DIR=/data/pd-diag
PROXY_LOG_DIR=/data/pd-diag/logs/proxy
