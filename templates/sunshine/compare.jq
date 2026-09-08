def require($ok; $message): if $ok then . else error($message) end;
def valid:
  .schema == 1 and (.provenance | type == "object") and
  (.provenance | keys | sort) == (["image_digest","sunshine_version","mesa_version","kernel","gpu_bdf",
    "game_revision","game_settings","client","network","capture","codec","resolution","fps","bitrate_mbps",
    "cache","warmup_seconds","sample_seconds","background_workload"] | sort) and
  (.provenance | [.[] | if type == "string" then length > 0 and (test("REPLACE|RECORD") | not)
      else type == "number" and . > 0 end] | all) and
  (.provenance.image_digest | test("^sha256:[a-f0-9]{64}$")) and
  (.provenance.cache == "cold" or .provenance.cache == "warm") and
  (.profile | keys | sort) == ["encoder","vaapi_strict_rc_buffer","vk_tune"] and
  (.profile.encoder == "vaapi" or .profile.encoder == "vulkan") and
  (.profile.vk_tune == 2 or .profile.vk_tune == 3) and
  (.profile.vaapi_strict_rc_buffer == "enabled" or .profile.vaapi_strict_rc_buffer == "disabled") and
  (.runs | type == "array" and length >= 3) and
  all(.runs[]; (keys | sort) == ["decode_p95_ms","dropped_frames_pct","encode_p95_ms","frame_p99_ms","gpu_power_w"] and
    all(.[]; . == null or (type == "number" and . >= 0)) and
    (.dropped_frames_pct == null or .dropped_frames_pct <= 100));
def stats:
  .runs as $runs | [($runs[0] | keys[]) as $k |
    [$runs[][$k] | select(. != null)] | sort as $v |
    {key:$k,value:(if ($v | length) == 0 then {n:0,median:null,min:null,max:null}
      else {n:($v|length), median:(($v[(($v|length)-1)/2|floor] + $v[($v|length)/2|floor])/2),min:$v[0],max:$v[-1]} end)}] | from_entries;
require(length == 2; "provide exactly two measurements") |
require(all(.[]; valid); "measurement schema or provenance is incomplete") |
require(.[0].provenance == .[1].provenance; "paired runs require identical workload/client/image/cache/capture provenance") |
require(([.[0].profile, .[1].profile] | . as $p | [($p[0]|keys[]) as $k | select($p[0][$k] != $p[1][$k])] | length) == 1;
  "change exactly one encoder profile setting") |
.[0] as $a | .[1] as $b | ($a | stats) as $sa | ($b | stats) as $sb |
require(any($sa | keys[]; . as $k | $sa[$k].n >= 3 and $sb[$k].n >= 3); "need at least one metric measured in three runs per profile") |
{schema:1,status:"operator-supplied-measurements-not-qualified",provenance:$a.provenance,
 baseline:{profile:$a.profile,metrics:$sa},candidate:{profile:$b.profile,metrics:$sb},
 note:"Per-run percentile summaries are not pooled percentiles. Null means unmeasured; no additive performance claims."}
