#!/usr/bin/env bash
# Reproduce every TLC run behind docs/state-model.md.
#
#   spec/run-tlc.sh              all runs
#   spec/run-tlc.sh 'live-*'     only runs whose name matches the glob
#
# Needs java (8 or later) and spec/tools/tla2tools.jar. If the jar is missing the
# script downloads release v1.7.4 and verifies its SHA-256.
# Output: spec/states/ (git-ignored): cfg/, logs/<run>.log, summary.tsv.
# Tunables: TLC_WORKERS (default 2), TLC_HEAP (default 2g), TLC_TIMEOUT (default 300 s per run).
# Exit status: 0 if every run gave the expected result, 1 otherwise.
set -u
cd "$(dirname "$0")" || exit 1

JAR=tools/tla2tools.jar
JAR_URL=https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar
JAR_SHA=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
FILTER=${1:-*}
WORKERS_N=${TLC_WORKERS:-2}
HEAP=${TLC_HEAP:-2g}
LIMIT=${TLC_TIMEOUT:-300}

if [ ! -f "$JAR" ]; then
  mkdir -p tools
  curl -fsSL -o "$JAR" "$JAR_URL" || { echo "download of tla2tools.jar failed" >&2; exit 1; }
fi
echo "$JAR_SHA  $JAR" | sha256sum -c --quiet || { echo "unexpected tla2tools.jar checksum" >&2; exit 1; }

mkdir -p states/cfg states/logs
SUMMARY=states/summary.tsv
printf 'run\tproperty\texpected\tresult\tgenerated\tdistinct\tseconds\n' >"$SUMMARY"
BAD=0

q() { # "a b c" -> "a", "b", "c"
  local out="" x
  for x in $1; do out="$out${out:+, }\"$x\""; done
  printf '%s' "$out"
}

ALL_FIXES="claimRecheck sharedPark skipAsks requiredBeforeApprove markedNotes parkOutsideFailure triageAck
 workerEffectiveCI atomicLabels matchHead signoffStrict roundCap pmEscalate pmSkipParked
 partialStaysOpen"

# run NAME EXPECT(pass|fail) PROPERTY... [key=value ...]
# keys: spec issues workers rounds env gated pm merge fixes
run() {
  local name=$1 expect=$2; shift 2
  local spec=Spec issues=1 workers=w1 rounds=2 env="" gated=FALSE pm=0 merge=signoff fixes=""
  local invs="" props="" a
  for a in "$@"; do
    case $a in
      spec=*) spec=${a#*=} ;;   issues=*) issues=${a#*=} ;;
      workers=*) workers=${a#*=} ;; rounds=*) rounds=${a#*=} ;;
      env=*) env=${a#*=} ;;     gated=*) gated=${a#*=} ;;
      pm=*) pm=${a#*=} ;;       merge=*) merge=${a#*=} ;;
      fixes=*) fixes=${a#*=} ;;
      *Progress|EscalationHappens) props="$props $a" ;;
      *) invs="$invs $a" ;;
    esac
  done
  # shellcheck disable=SC2254
  case $name in $FILTER) ;; *) return ;; esac
  [ "$fixes" = ALL ] && fixes=$ALL_FIXES

  local cfg=states/cfg/$name.cfg log=states/logs/$name.log
  {
    echo "SPECIFICATION $spec"
    echo "CONSTANTS"
    echo "    Issues = {${issues// /, }}"
    echo "    Workers = {${workers// /, }}"
    echo "    MaxRounds = $rounds"
    echo "    Env = {$(q "$env")}"
    echo "    Gated = $gated"
    echo "    PmSlots = $pm"
    echo "    MergeMode = \"$merge\""
    echo "    Fixes = {$(q "$fixes")}"
    for a in $invs; do echo "INVARIANT $a"; done
    for a in $props; do echo "PROPERTY $a"; done
    echo "CHECK_DEADLOCK FALSE"
  } >"$cfg"
  cp "$cfg" "states/$name.cfg"   # TLC resolves -config next to the module

  local start=$SECONDS rc result gen dist
  timeout "$LIMIT" nice -n 10 java -XX:+UseParallelGC "-Xmx$HEAP" -cp "$JAR" tlc2.TLC \
    -workers "$WORKERS_N" -lncheck final -metadir "states/meta/$name" \
    -config "states/$name.cfg" devloops.tla >"$log" 2>&1
  rc=$?
  rm -rf "states/meta/$name" "states/$name.cfg"
  if grep -q 'Model checking completed. No error has been found' "$log"; then result=pass
  elif grep -qE 'is violated|properties were violated' "$log"; then result=fail
  elif [ $rc -eq 124 ]; then result=timeout
  else result=error
  fi
  gen=$(grep -oE '^[0-9,]+ states generated' "$log" | tail -1 | cut -d' ' -f1)
  dist=$(grep -oE '[0-9,]+ distinct states found' "$log" | tail -1 | cut -d' ' -f1)
  [ "$result" = "$expect" ] || BAD=1
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$(echo $invs $props)" "$expect" "$result" \
    "${gen:--}" "${dist:--}" "$((SECONDS - start))" | tee -a "$SUMMARY"
}

BASE="blocker defect ownerComment ciFail needsUser implFail stale"

# --- 1. Safety, model of the code as it is: properties that hold --------------
run main-typeok-issue    pass TypeOK OnlyOneMayPush workers="w1 w2" env="$BASE"
run main-typeok-env      pass TypeOK OnlyOneMayPush workers="w1 w2" gated=TRUE \
    env="conflict baseBreak cancelled crash rejected"
run main-typeok-qa       pass TypeOK OnlyOneMayPush workers="w1 w2" \
    env="qaFinding qaHuman crash partial"
run main-typeok-pm       pass TypeOK pm=1 env="defect conflict ownerComment baseBreak qaHuman partial"

# --- 2. Safety, as it is: violations ------------------------------------------
run merge-signoff        fail MergedOnlyWithSignoffs env="conflict"
run merge-labels         fail MergedOnlyWithSignoffs merge=labels fixes=matchHead env="ownerComment"
run merge-signoff-matchhead pass MergedOnlyWithSignoffs fixes=matchHead \
    env="defect ownerComment conflict ciFail qaFinding"
run merge-green          fail MergedOnlyGreen env="conflict"
run merge-green-matchhead fail MergedOnlyGreen fixes=matchHead gated=TRUE env=""
run gated-approve        fail ApproveOnlyWithRequiredChecks gated=TRUE env=""
run labels-two           fail OneVerdictLabel env="conflict qaFinding"
run labels-two-settled   pass OneVerdictLabelSettled env="defect conflict ownerComment qaFinding qaHuman"
run claim-two-impl       fail OneImplementer workers="w1 w2" env="stale"
run claim-no-env         pass OneImplementer workers="w1 w2" env=""
run claim-may-push       pass OnlyOneMayPush workers="w1 w2" env="stale crash"
run dup-pr               fail NoDuplicatePR workers="w1 w2" env=""
run parked-slot          fail ParkedStaysParked workers="w1 w2" env="needsUser"
run round-limit          fail RoundLimit env="ownerComment defect conflict"
run partial-close        fail ClosedOnlyWhenComplete env="partial"
run stall-skip           fail NoSilentStall env="implFail"
run stall-pm-escalate    fail NoSilentStall pm=1 rounds=1 env="defect"
run stall-pm-escalate-fixed pass NoSilentStall pm=1 rounds=1 env="defect" fixes=pmEscalate
run owner-routine        fail OwnerOnlyExplicit env=""

# --- 3. Liveness, as it is ----------------------------------------------------
run live-baseline-issue  pass IssueProgress PRProgress QaProgress EscalationHappens \
    spec=LiveSpec env="blocker"
run live-baseline-review pass IssueProgress PRProgress QaProgress EscalationHappens \
    spec=LiveSpec env="defect conflict ciFail"
run live-baseline-qa     pass IssueProgress PRProgress QaProgress EscalationHappens \
    spec=LiveSpec env="qaFinding needsUser"
run live-skip            fail IssueProgress spec=LiveSpec env="implFail"
run live-base-break      fail PRProgress spec=LiveSpec env="baseBreak"
run live-owner-comment   fail PRProgress spec=LiveSpec env="ownerComment baseBreak"
run live-starve-comment  fail IssueProgress spec=NoMergeSpec issues="1 2" env="ownerComment"
run live-starve-superseded fail IssueProgress spec=NoMergeSpec issues="1 2" env="cancelled"
run live-nomerge-baseline pass IssueProgress spec=NoMergeSpec issues="1 2" env=""
run live-pm-escalate     fail EscalationHappens spec=LiveSpec pm=1 rounds=1 env="defect"
run live-pm-qa           fail QaProgress spec=NoMergeSpec pm=1 issues="1 2" env="cancelled"
run live-pm-base-break   fail PRProgress spec=LiveSpec pm=1 env="baseBreak"

# --- 4. The same scenarios with the model-only fixes ---------------------------
run fix-merge            pass MergedOnlyWithSignoffs MergedOnlyGreen ApproveOnlyWithRequiredChecks \
    fixes=ALL gated=TRUE env="defect ownerComment conflict ciFail qaFinding"
run fix-safety-issue     pass TypeOK OnlyOneMayPush NoDuplicatePR ParkedStaysParked \
    ClosedOnlyWhenComplete NoSilentStall OneVerdictLabel \
    fixes=ALL workers="w1 w2" env="$BASE partial"
run fix-safety-pr-review pass TypeOK MergedOnlyWithSignoffs MergedOnlyGreen \
    ApproveOnlyWithRequiredChecks OneVerdictLabel RoundLimit NoSilentStall \
    fixes=ALL gated=TRUE env="defect ownerComment conflict ciFail qaFinding qaHuman"
run fix-safety-pr-ci     pass TypeOK MergedOnlyWithSignoffs MergedOnlyGreen \
    ApproveOnlyWithRequiredChecks OneVerdictLabel RoundLimit NoSilentStall \
    fixes=ALL gated=TRUE env="defect conflict baseBreak cancelled qaHuman"
run fix-safety-pm        pass TypeOK NoSilentStall RoundLimit fixes=ALL pm=1 rounds=1 \
    env="defect ownerComment baseBreak needsUser implFail"
run fix-claim-stale      fail OneImplementer fixes=ALL workers="w1 w2" env="stale"
run fix-owner-routine    fail OwnerOnlyExplicit fixes=ALL env=""
run fix-live-issue       pass IssueProgress PRProgress QaProgress EscalationHappens \
    spec=LiveSpec fixes=ALL env="blocker implFail needsUser stale crash rejected"
run fix-live-comment     pass IssueProgress PRProgress QaProgress EscalationHappens \
    spec=LiveSpec fixes=ALL env="defect ownerComment baseBreak"
run fix-live-ci          pass IssueProgress PRProgress QaProgress EscalationHappens \
    spec=LiveSpec fixes=ALL gated=TRUE env="conflict baseBreak cancelled qaHuman"
run fix-live-rounds      pass IssueProgress PRProgress QaProgress EscalationHappens \
    spec=LiveSpec fixes=ALL rounds=1 env="defect ciFail conflict qaFinding partial"
run fix-live-starve-comment pass IssueProgress spec=NoMergeSpec fixes=ALL issues="1 2" env="ownerComment"
run fix-live-starve-superseded pass IssueProgress spec=NoMergeSpec fixes=ALL issues="1 2" env="cancelled"
run fix-live-pm          pass IssueProgress PRProgress QaProgress EscalationHappens \
    spec=LiveSpec fixes=ALL pm=1 rounds=1 env="defect baseBreak needsUser implFail"
run fix-live-pm-qa       pass QaProgress spec=NoMergeSpec fixes=ALL pm=1 issues="1 2" env="cancelled needsUser"

# --- 5. One fix at a time: which single change removes which counterexample ----
run one-merge-matchhead-strict pass MergedOnlyWithSignoffs MergedOnlyGreen \
    fixes="matchHead signoffStrict" env="conflict"
run one-gated-approve    pass ApproveOnlyWithRequiredChecks gated=TRUE env="" fixes=requiredBeforeApprove
run one-labels           pass OneVerdictLabel env="conflict qaFinding" fixes=atomicLabels
run one-dup-pr           pass NoDuplicatePR workers="w1 w2" env="" fixes=claimRecheck
run one-dup-pr-stale     pass NoDuplicatePR workers="w1 w2" env="stale crash" fixes=claimRecheck
run one-parked           pass ParkedStaysParked workers="w1 w2" env="needsUser" fixes=sharedPark
run one-round-limit      pass RoundLimit env="ownerComment defect conflict" fixes=roundCap
run one-partial          pass ClosedOnlyWhenComplete env="partial" fixes=partialStaysOpen
run one-stall-skip       pass NoSilentStall env="implFail" fixes=skipAsks
run one-live-skip        pass IssueProgress spec=LiveSpec env="implFail" fixes=skipAsks
run one-live-base-marked pass PRProgress spec=LiveSpec env="baseBreak" fixes=markedNotes
run one-live-base-triage fail PRProgress spec=LiveSpec env="baseBreak" fixes=triageAck
run one-live-comment-marked fail PRProgress spec=LiveSpec env="ownerComment baseBreak" fixes=markedNotes
run one-live-comment-triage fail PRProgress spec=LiveSpec env="ownerComment baseBreak" fixes=triageAck
run one-live-comment-both pass PRProgress spec=LiveSpec env="ownerComment baseBreak" \
    fixes="markedNotes triageAck"
run one-starve-comment   pass IssueProgress spec=NoMergeSpec issues="1 2" env="ownerComment" fixes=triageAck
run one-starve-superseded pass IssueProgress spec=NoMergeSpec issues="1 2" env="cancelled" fixes=workerEffectiveCI
run one-pm-escalate      pass EscalationHappens spec=LiveSpec pm=1 rounds=1 env="defect" fixes=pmEscalate
run one-pm-qa            pass QaProgress spec=NoMergeSpec pm=1 issues="1 2" env="cancelled" fixes=workerEffectiveCI
run one-pm-base-marked   fail PRProgress spec=LiveSpec pm=1 env="baseBreak" fixes="markedNotes triageAck"
run one-pm-base-park     pass PRProgress spec=LiveSpec pm=1 env="baseBreak" fixes="markedNotes parkOutsideFailure"
run sanity-merge-asis    fail NeverMerged env=""
run sanity-merge-fixed   fail NeverMerged fixes=ALL gated=TRUE env=""

echo
echo "summary: spec/$SUMMARY"
[ $BAD -eq 0 ] && echo "all runs gave the expected result" || echo "SOME RUNS DID NOT GIVE THE EXPECTED RESULT"
exit $BAD
