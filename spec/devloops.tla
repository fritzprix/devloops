------------------------------ MODULE devloops ------------------------------
(***************************************************************************)
(* State model of the devloops loops: worker, gatekeeper, qa, pm,          *)
(* producer (as the origin of unjudged issues) and the human owner.        *)
(* The transition table with file:line evidence is docs/state-model.md.    *)
(*                                                                         *)
(* With Fixes = {} the model follows the skills and devloops_lib as they   *)
(* are on this branch. Each name in Fixes switches ONE hypothetical change *)
(* on. The changes exist only in this model; they show which behaviour     *)
(* removes a counterexample. No skill or code file was changed.            *)
(*                                                                         *)
(* Abstractions (see docs/state-model.md, "Model scope"):                  *)
(*  - A head SHA is not stored. Every "marker is at the current head" is a *)
(*    boolean that every push sets to FALSE.                               *)
(*  - One PR per issue is stored; a second one only raises ghost "dupPR".  *)
(*  - Every environment event named in Env can happen at most once.        *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    Issues,     \* set of issue numbers (naturals; lower number = first in plan)
    Workers,    \* set of worker slots
    MaxRounds,  \* gatekeeper.max_rounds
    Env,        \* enabled one-shot events, see EnvNames
    Gated,      \* TRUE: a required check runs only after gatekeeper:review-clean
    PmSlots,    \* 0: every loop has its own scheduler; 1: pm-loop cascade, slots: 1
    MergeMode,  \* "labels" or "signoff": what the owner looks at before a merge
    Fixes       \* set of hypothetical fixes, see FixNames

EnvNames == {"blocker", "defect", "ownerComment", "conflict", "ciFail",
             "baseBreak", "cancelled", "qaFinding", "qaHuman", "needsUser",
             "implFail", "stale", "crash", "partial", "rejected"}

FixNames == {"claimRecheck",     \* claim.py refuses an issue that has an open PR
             "sharedPark",       \* needsUser / skipped are shared by all slots
             "skipAsks",         \* "failed twice" asks the owner, not a silent skip
             "requiredBeforeApprove", \* no approve until required checks passed
             "markedNotes",      \* gatekeeper comments carry a marker
     "parkOutsideFailure", \* "reviewed, CI broken outside the diff" leaves the review queue
             "triageAck",        \* an answered PR comment stops being "new"
             "workerEffectiveCI",\* worker ignores superseded runs and unfixable base breaks
             "atomicLabels",     \* verdict label swap is one write
             "matchHead",        \* merge only the head that signoff checked
             "signoffStrict",    \* signoff also needs green CI and no escalation
             "roundCap",         \* no verdict past the round limit
             "pmEscalate",       \* PM counts an unlabelled "escalate" PR as review work
             "pmSkipParked",     \* PM does not count parked issues as worker backlog
             "partialStaysOpen"} \* PM does not close an issue for a partial PR

ASSUME /\ Env \subseteq EnvNames
       /\ Fixes \subseteq FixNames
       /\ MergeMode \in {"labels", "signoff"}
       /\ PmSlots \in {0, 1}
       /\ Gated \in BOOLEAN
       /\ MaxRounds \in Nat \ {0}

Fix(f) == f \in Fixes
Free == "free"

VARIABLES iss,      \* [Issues -> issue record]
          pr,       \* [Issues -> PR record]
          wk,       \* [Workers -> worker record]
          bud,      \* events of Env that have not happened yet
          baseOk,   \* FALSE while the base branch is broken
          ghost     \* history flags for properties

vars == <<iss, pr, wk, bud, baseOk, ghost>>

----------------------------------------------------------------------------
(* Types *)

NoPR == [st |-> "none", partial |-> FALSE,
         ci |-> "pending", cancelled |-> FALSE, gated |-> "na",
         defect |-> FALSE, conflict |-> FALSE,
         gv |-> "none", gAt |-> FALSE, resp |-> FALSE, rounds |-> 0,
         cleanAt |-> FALSE, gl |-> {}, glOld |-> {}, gk |-> "idle",
         qv |-> "none", qAt |-> FALSE, qResp |-> FALSE, ql |-> {}, qlOld |-> {},
         gNew |-> FALSE, wNew |-> "none", qNew |-> FALSE, own |-> "no"]

GkLabels == {"approved", "cr", "escalated"}
QaLabels == {"accepted", "cr", "nh"}
GhostFlags == {"dupPR", "parkBreach", "overRound", "mergeNoSignoff",
               "mergeRed", "closedIncomplete", "approveUngated"}

PRType == [st : {"none", "open", "merged"}, partial : BOOLEAN,
           ci : {"pending", "pass", "fail", "base"}, cancelled : BOOLEAN,
           gated : {"na", "notrun", "pending", "pass"},
           defect : BOOLEAN, conflict : BOOLEAN,
           gv : {"none", "cr", "approve", "escalate"}, gAt : BOOLEAN,
           resp : BOOLEAN, rounds : 0..(MaxRounds + 1),
           cleanAt : BOOLEAN, gl : SUBSET GkLabels, glOld : SUBSET GkLabels,
           gk : {"idle", "checked", "stale"},
           qv : {"none", "cr", "accepted", "nh"}, qAt : BOOLEAN,
           qResp : BOOLEAN, ql : SUBSET QaLabels, qlOld : SUBSET QaLabels,
           gNew : BOOLEAN, wNew : {"none", "info", "fix"}, qNew : BOOLEAN,
           own : {"no", "fresh", "stale"}]

IssueType == [open : BOOLEAN, approved : BOOLEAN, blocker : BOOLEAN,
              vl : SUBSET {"validated", "rejected"},
              claim : Workers \cup {Free}, stale : BOOLEAN,
              skip : SUBSET Workers, ask : SUBSET Workers]

WorkerType == [ph : {"idle", "picked", "impl"}, i : Issues \cup {0}]

TypeOK == /\ iss \in [Issues -> IssueType]
          /\ pr \in [Issues -> PRType]
          /\ wk \in [Workers -> WorkerType]
          /\ bud \subseteq Env
          /\ baseOk \in BOOLEAN
          /\ ghost \subseteq GhostFlags

Init ==
    /\ iss \in [Issues ->
          {[open |-> TRUE, approved |-> FALSE, blocker |-> b, vl |-> {},
            claim |-> Free, stale |-> FALSE, skip |-> {}, ask |-> {}] :
              b \in (IF "blocker" \in Env THEN BOOLEAN ELSE {FALSE})}]
    /\ pr = [i \in Issues |-> NoPR]
    /\ wk = [w \in Workers |-> [ph |-> "idle", i |-> 0]]
    /\ bud = Env
    /\ baseOk = TRUE
    /\ ghost = {}

----------------------------------------------------------------------------
(* Derived views: each mirrors one classifier of devloops_lib *)

IsOpen(i) == pr[i].st = "open"

\* gatekeeper_queue.py:283-309,501-504: superseded runs of a required check are ignored.
GkFailing(p) == p.ci \in {"fail", "base"}
GkPending(p) == p.ci = "pending" \/ p.gated = "pending"
\* gatekeeper_queue.py:312-328
GkMissing(p) == p.gated \in {"notrun", "pending"}

\* gatekeeper_queue.py:373-438 classify()
GkStatus(i) ==
    LET p == pr[i] IN
    IF "escalated" \in p.gl THEN "escalate"
    ELSE IF p.conflict THEN "wait-worker"
    ELSE IF p.gNew /\ ~(Fix("roundCap") /\ p.rounds >= MaxRounds) THEN "review"
    ELSE IF p.gv = "approve" /\ p.gAt /\ ~(Fix("roundCap") /\ GkFailing(p) /\ p.rounds >= MaxRounds)
         THEN IF GkFailing(p) THEN "review" ELSE "approved"
    ELSE IF p.rounds >= MaxRounds THEN "escalate"
    ELSE IF p.gv = "cr" /\ p.gAt THEN IF p.resp THEN "review" ELSE "wait-worker"
    ELSE IF p.gv # "none" /\ ~p.cleanAt THEN "review"
    ELSE IF Fix("parkOutsideFailure") /\ p.cleanAt /\ p.ci = "base" THEN "await-ci"
    ELSE IF p.cleanAt
         THEN IF GkFailing(p) THEN "review"
              ELSE IF GkPending(p) \/ GkMissing(p) THEN "await-ci"
              ELSE "review"
    ELSE "review"

\* pr_status.py:294 does not use effective_checks(): a superseded run counts.
WFailing(p) ==
    IF Fix("workerEffectiveCI")
    THEN p.ci = "fail" \/ (p.ci = "base" /\ baseOk)
    ELSE p.ci \in {"fail", "base"} \/ p.cancelled

\* pr_status.py:104-108 / 130-134 and 239-271
NextAction(i) ==
    LET p == pr[i] IN
    IF p.st # "open" THEN "none"
    ELSE IF "escalated" \in p.gl THEN "none"
    ELSE IF p.conflict THEN "resolve"
    ELSE IF p.gv = "cr" /\ p.gAt /\ ~p.resp /\ ~p.cleanAt THEN "addrG"
    ELSE IF p.qv = "cr" /\ p.qAt /\ ~p.qResp THEN "addrQ"
    ELSE IF p.wNew # "none" THEN "triage"
    ELSE IF WFailing(p) THEN "fixci"
    ELSE "none"

Rank(a) == CASE a = "resolve" -> 1 [] a = "addrG" -> 2 [] a = "addrQ" -> 3
             [] a = "triage" -> 4 [] a = "fixci" -> 5 [] OTHER -> 9

\* qa.py:97-110 classify_pr()
QaStatus(i) ==
    LET p == pr[i] IN
    IF p.qv = "none" \/ ~p.qAt \/ p.qNew THEN "review"
    ELSE IF p.qv = "cr" THEN IF p.qResp THEN "review" ELSE "wait-worker"
    ELSE IF p.qv = "nh" THEN "needs-human"
    ELSE "accepted"

\* Slot state: needsUser and skipped are per slot (config.py:514-525).
Asked(w, i) == IF Fix("sharedPark") THEN iss[i].ask # {} ELSE w \in iss[i].ask
Skipped(w, i) == IF Fix("sharedPark") THEN iss[i].skip # {} ELSE w \in iss[i].skip

PlanPRs(w) == {i \in Issues : NextAction(i) # "none" /\ ~Asked(w, i)}
\* plan_tick.py:76-97: PR items first, ordered by (action rank, number).
FirstPR(w, i) ==
    /\ i \in PlanPRs(w)
    /\ \A j \in PlanPRs(w) :
          \/ Rank(NextAction(i)) < Rank(NextAction(j))
          \/ Rank(NextAction(i)) = Rank(NextAction(j)) /\ i <= j

\* worker_candidates.py:62-109 and issue_candidates.py:184-189
Candidate(i) == /\ iss[i].open /\ iss[i].approved
                /\ "rejected" \notin iss[i].vl
                /\ ~iss[i].blocker
                /\ ~IsOpen(i)

----------------------------------------------------------------------------
(* pm-loop cascade, pm_pipeline.py:56-99 and pm_schedule.py:119-157 *)

PmReview ==
    \E i \in Issues : IsOpen(i) /\ "escalated" \notin pr[i].gl /\
        \/ GkStatus(i) = "review"
        \/ Fix("pmEscalate") /\ GkStatus(i) = "escalate"
PmWork ==
    \/ \E i \in Issues : NextAction(i) # "none"
    \* approvedIssues drops `skipped` entries (of the default slot; exact for one
    \* worker) but knows nothing about needsUser.
    \/ \E i \in Issues : Candidate(i) /\ iss[i].skip # Workers /\
          (Fix("pmSkipParked") => iss[i].ask = {} /\ iss[i].skip = {})

WorkerRuns == PmSlots = 0 \/ ~PmReview
QaRuns == PmSlots = 0 \/ (~PmReview /\ ~PmWork)
\* Under the PM the gatekeeper ticks only when the plan includes it.
GkTick == PmSlots = 0 \/ PmReview

----------------------------------------------------------------------------
(* Helpers *)

\* A push: new head. gated job needs the label again (comment-format.md:143-145).
Pushed(p, d) ==
    [p EXCEPT !.ci = "pending", !.cancelled = FALSE,
              !.gated = IF Gated THEN "notrun" ELSE "na",
              !.defect = d, !.conflict = FALSE,
              !.gAt = FALSE, !.qAt = FALSE, !.cleanAt = FALSE,
              !.wNew = "none",
              !.gk = IF p.gk = "idle" THEN "idle" ELSE "stale",
              !.own = IF p.own = "no" THEN "no" ELSE "stale"]

\* A push can carry a new defect once.
PushDefects == IF "defect" \in bud THEN BOOLEAN ELSE {FALSE}
UseIf(cond, e) == IF cond THEN bud \ {e} ELSE bud

ParkGhost(w, i) ==
    IF iss[i].ask \ {w} # {} THEN ghost \cup {"parkBreach"} ELSE ghost

----------------------------------------------------------------------------
(* Worker: PR service (worker-loop/SKILL.md:97-111), one item per tick *)

DoPush(w, i, p) ==
    \E d \in PushDefects :
        /\ pr' = [pr EXCEPT ![i] = Pushed(p, d)]
        /\ bud' = UseIf(d, "defect")
        /\ ghost' = ParkGhost(w, i)
        /\ UNCHANGED <<iss, wk, baseOk>>

\* address-gatekeeper: fix and push, or decline; both end with a response marker.
AddrG(w, i) ==
    /\ NextAction(i) = "addrG"
    /\ \/ DoPush(w, i, [pr[i] EXCEPT !.resp = TRUE, !.gNew = FALSE])
       \/ /\ pr' = [pr EXCEPT ![i].resp = TRUE, ![i].gNew = FALSE]
          /\ ghost' = ParkGhost(w, i)
          /\ UNCHANGED <<iss, wk, bud, baseOk>>

AddrQ(w, i) ==
    /\ NextAction(i) = "addrQ"
    /\ \/ DoPush(w, i, [pr[i] EXCEPT !.qResp = TRUE])
       \/ /\ pr' = [pr EXCEPT ![i].qResp = TRUE]
          /\ ghost' = ParkGhost(w, i)
          /\ UNCHANGED <<iss, wk, bud, baseOk>>

\* resolve-conflict: mechanical rebase (changes no behaviour, so a defect stays),
\* or needsUser for a non-mechanical hunk.
Resolve(w, i) ==
    /\ NextAction(i) = "resolve"
    /\ \/ /\ pr' = [pr EXCEPT ![i] = Pushed(@, @.defect)]
          /\ ghost' = ParkGhost(w, i)
          /\ UNCHANGED <<iss, wk, bud, baseOk>>
       \/ /\ "needsUser" \in bud
          /\ iss' = [iss EXCEPT ![i].ask = @ \cup {w}]
          /\ bud' = bud \ {"needsUser"}
          /\ ghost' = ParkGhost(w, i)
          /\ UNCHANGED <<pr, wk, baseOk>>

\* triage-comments: fix valid items (push) or reply with a note. pr_status.py:336-350
\* keeps the comment "new" until a later commit exists, so a reply changes nothing.
Triage(w, i) ==
    /\ NextAction(i) = "triage"
    /\ IF pr[i].wNew = "fix"
       THEN DoPush(w, i, pr[i])
       ELSE /\ Fix("triageAck")
            /\ pr' = [pr EXCEPT ![i].wNew = "none"]
            /\ ghost' = ParkGhost(w, i)
            /\ UNCHANGED <<iss, wk, bud, baseOk>>

\* fix-ci: push only for a reproduced cause; never rerun to get green.
FixCI(w, i) ==
    /\ NextAction(i) = "fixci"
    /\ \/ pr[i].ci = "fail"
       \/ pr[i].ci = "base" /\ baseOk       \* rebase onto the repaired base
    /\ DoPush(w, i, pr[i])

WorkerPR(w, i) ==
    /\ WorkerRuns
    /\ wk[w].ph = "idle"
    /\ FirstPR(w, i)
    /\ \/ AddrG(w, i) \/ AddrQ(w, i) \/ Resolve(w, i)
       \/ Triage(w, i) \/ FixCI(w, i)

----------------------------------------------------------------------------
(* Worker: issues (worker-loop/SKILL.md:115-160, claims.py) *)

\* plan + --check-issue; PR items come first and one item is done per tick.
Pick(w, i) ==
    /\ WorkerRuns
    /\ wk[w].ph = "idle"
    /\ PlanPRs(w) = {}
    /\ Candidate(i) /\ ~Asked(w, i) /\ ~Skipped(w, i)
    /\ wk' = [wk EXCEPT ![w] = [ph |-> "picked", i |-> i]]
    /\ ghost' = ParkGhost(w, i)
    /\ UNCHANGED <<iss, pr, bud, baseOk>>

Idle == [ph |-> "idle", i |-> 0]

\* claims.py:267-296: compare-and-swap on wip/issue-N; no look at open PRs.
Claim(w) ==
    LET i == wk[w].i IN
    /\ wk[w].ph = "picked"
    /\ IF /\ iss[i].claim \in {Free, w} \/ iss[i].stale
          /\ Fix("claimRecheck") => ~IsOpen(i)
       THEN /\ iss' = [iss EXCEPT ![i].claim = w, ![i].stale = FALSE]
            /\ wk' = [wk EXCEPT ![w].ph = "impl"]
       ELSE /\ wk' = [wk EXCEPT ![w] = Idle]
            /\ UNCHANGED iss
    /\ UNCHANGED <<pr, bud, baseOk, ghost>>

Release(i) == [iss EXCEPT ![i].claim = Free, ![i].stale = FALSE]

NewPR(part, d) == [NoPR EXCEPT !.st = "open", !.partial = part, !.defect = d,
                               !.gated = IF Gated THEN "notrun" ELSE "na"]

\* guard.py:55-60: push and `pr create` run only while the claim is ours.
CreatePR(w) ==
    LET i == wk[w].i IN
    /\ wk[w].ph = "impl" /\ iss[i].claim = w
    /\ \E d \in PushDefects, part \in (IF "partial" \in bud THEN BOOLEAN ELSE {FALSE}) :
        /\ IF IsOpen(i)
           THEN /\ ghost' = ghost \cup {"dupPR"}
                /\ UNCHANGED <<pr, bud>>
           ELSE /\ pr' = [pr EXCEPT ![i] = NewPR(part, d)]
                /\ bud' = (IF d THEN bud \ {"defect"} ELSE bud) \
                          (IF part THEN {"partial"} ELSE {})
                /\ UNCHANGED ghost
    /\ iss' = Release(i)
    /\ wk' = [wk EXCEPT ![w] = Idle]
    /\ UNCHANGED baseOk

Lost(w) ==
    LET i == wk[w].i IN
    /\ wk[w].ph = "impl" /\ iss[i].claim # w
    /\ wk' = [wk EXCEPT ![w] = Idle]
    /\ UNCHANGED <<iss, pr, bud, baseOk, ghost>>

\* worker-loop/SKILL.md:160: record `skipped`; a comment is optional.
ImplFail(w) ==
    LET i == wk[w].i IN
    /\ wk[w].ph = "impl" /\ iss[i].claim = w /\ "implFail" \in bud
    /\ iss' = [Release(i) EXCEPT ![i].skip = @ \cup {w},
                    ![i].ask = IF Fix("skipAsks") THEN @ \cup {w} ELSE @]
    /\ bud' = bud \ {"implFail"}
    /\ wk' = [wk EXCEPT ![w] = Idle]
    /\ UNCHANGED <<pr, baseOk, ghost>>

ImplAsk(w) ==
    LET i == wk[w].i IN
    /\ wk[w].ph = "impl" /\ iss[i].claim = w /\ "needsUser" \in bud
    /\ iss' = [Release(i) EXCEPT ![i].ask = @ \cup {w}]
    /\ bud' = bud \ {"needsUser"}
    /\ wk' = [wk EXCEPT ![w] = Idle]
    /\ UNCHANGED <<pr, baseOk, ghost>>

WorkerImpl(w) == Claim(w) \/ CreatePR(w) \/ Lost(w)

----------------------------------------------------------------------------
(* Gatekeeper (gatekeeper-loop/SKILL.md:81-165, forge/cli.py:87-123) *)

\* set_verdict_labels: add the new label first, remove the others second.
SwapG(p, add, remove) ==
    IF Fix("atomicLabels")
    THEN [p EXCEPT !.gl = (@ \cup add) \ remove, !.glOld = {}]
    ELSE [p EXCEPT !.gl = @ \cup add, !.glOld = (remove \cap p.gl) \ add]

\* check_head (gatekeeper_queue.py:655-673); the write is a later step.
GkCheck(i) ==
    /\ GkTick /\ IsOpen(i) /\ GkStatus(i) = "review"
    /\ pr[i].gk # "checked"
    /\ pr' = [pr EXCEPT ![i].gk = "checked"]
    /\ UNCHANGED <<iss, wk, bud, baseOk, ghost>>

Verdict(i, p, v, lab) ==
    LET q == [p EXCEPT !.gv = v, !.gAt = (pr[i].gk = "checked"), !.resp = FALSE,
                       !.rounds = IF @ > MaxRounds THEN @ ELSE @ + 1,
                       !.cleanAt = FALSE, !.gNew = FALSE, !.gk = "idle"]
    IN /\ pr' = [pr EXCEPT ![i] = SwapG(q, {lab}, GkLabels \ {lab})]
       /\ ghost' = ghost
             \cup (IF pr[i].rounds >= MaxRounds /\ v # "escalate"
                   THEN {"overRound"} ELSE {})
             \cup (IF v = "approve" /\ GkMissing(pr[i])
                   THEN {"approveUngated"} ELSE {})
       /\ UNCHANGED <<iss, wk, bud, baseOk>>

GkCR(i) ==
    /\ pr[i].defect \/ pr[i].ci = "fail"
    /\ Verdict(i, pr[i], "cr", "cr")

GkApprove(i) ==
    /\ ~pr[i].defect /\ ~GkFailing(pr[i]) /\ ~GkPending(pr[i]) /\ ~pr[i].conflict
    /\ Fix("requiredBeforeApprove") => ~GkMissing(pr[i])
    /\ Verdict(i, pr[i], "approve", "approved")

\* verdict=clean: not a round; `set-verdict clean` triggers the gated job.
GkClean(i) ==
    LET p == pr[i]
        q == [p EXCEPT !.cleanAt = (p.gk = "checked"), !.gNew = FALSE, !.gk = "idle",
                       !.gated = IF @ = "notrun" THEN "pending" ELSE @]
    IN
    /\ ~p.defect /\ ~GkFailing(p)
    /\ GkPending(p) \/ (Fix("requiredBeforeApprove") /\ GkMissing(p))
    /\ pr' = [pr EXCEPT ![i] = SwapG(q, {}, {"cr"})]
    /\ UNCHANGED <<iss, wk, bud, baseOk, ghost>>

\* A CI failure outside the diff gets a plain comment (SKILL.md:93,140). An
\* unmarked comment by @me counts as an owner comment in all three classifiers.
GkOutsideComment(i) ==
    LET p == pr[i] IN
    /\ ~p.defect /\ p.ci = "base"
    /\ LET q == IF Fix("markedNotes")
                THEN [p EXCEPT !.gNew = FALSE, !.gk = "idle"]
                ELSE [p EXCEPT !.gNew = TRUE, !.wNew = IF @ = "fix" THEN @ ELSE "info",
                               !.qNew = TRUE, !.gk = "idle"]
       IN pr' = [pr EXCEPT ![i] =
                   IF Fix("parkOutsideFailure")
                   THEN [q EXCEPT !.cleanAt = (p.gk = "checked")]
                   ELSE q]
    /\ UNCHANGED <<iss, wk, bud, baseOk, ghost>>

GkWrite(i) ==
    /\ GkTick /\ IsOpen(i) /\ pr[i].gk # "idle" /\ pr[i].glOld = {}
    /\ \/ GkCR(i) \/ GkApprove(i) \/ GkClean(i) \/ GkOutsideComment(i)
       \* The state moved and no verdict fits any more: drop the review.
       \/ /\ GkStatus(i) # "review" \/ pr[i].ci = "pending"
          /\ pr' = [pr EXCEPT ![i].gk = "idle"]
          /\ UNCHANGED <<iss, wk, bud, baseOk, ghost>>

\* Round limit reached: label first, then the escalate marker.
GkEscalate(i) ==
    /\ GkTick /\ IsOpen(i) /\ GkStatus(i) = "escalate"
    /\ "escalated" \notin pr[i].gl /\ pr[i].gk = "idle" /\ pr[i].glOld = {}
    /\ pr' = [pr EXCEPT ![i] =
          SwapG([@ EXCEPT !.gv = "escalate", !.gAt = TRUE, !.resp = FALSE,
                          !.rounds = IF @ > MaxRounds THEN @ ELSE @ + 1,
                          !.cleanAt = FALSE, !.gNew = FALSE],
                {"escalated"}, GkLabels \ {"escalated"})]
    /\ UNCHANGED <<iss, wk, bud, baseOk, ghost>>

\* Second half of the label swap, stale approve cleanup (SKILL.md:95).
GkLabels2(i) ==
    /\ IsOpen(i)
    /\ \/ /\ pr[i].glOld # {}
          /\ pr' = [pr EXCEPT ![i].gl = @ \ pr[i].glOld, ![i].glOld = {}]
       \/ /\ GkTick /\ pr[i].glOld = {} /\ "approved" \in pr[i].gl
          /\ GkStatus(i) # "approved" /\ "escalated" \notin pr[i].gl
          /\ pr' = [pr EXCEPT ![i].gl =
                       (@ \ {"approved"}) \cup (IF GkFailing(pr[i]) THEN {"cr"} ELSE {})]
       \/ /\ GkTick /\ pr[i].glOld = {} /\ GkStatus(i) = "approved"
          /\ pr[i].gl # {"approved"}
          /\ pr' = [pr EXCEPT ![i].gl = {"approved"}]
    /\ UNCHANGED <<iss, wk, bud, baseOk, ghost>>

\* Issue validation (SKILL.md:167-190). Adds one of validated / rejected.
GkValidate(i) ==
    /\ iss[i].open /\ iss[i].vl = {}
    /\ \E v \in (IF "rejected" \in bud THEN {"validated", "rejected"} ELSE {"validated"}) :
          /\ iss' = [iss EXCEPT ![i].vl = {v}]
          /\ bud' = UseIf(v = "rejected", "rejected")
    /\ UNCHANGED <<pr, wk, baseOk, ghost>>

GkVerdictStep(i) == GkCheck(i) \/ GkWrite(i) \/ GkEscalate(i)

----------------------------------------------------------------------------
(* qa (qa-loop/SKILL.md:75-113) *)

SwapQ(p, add) ==
    IF Fix("atomicLabels")
    THEN [p EXCEPT !.ql = {add}, !.qlOld = {}]
    ELSE [p EXCEPT !.ql = @ \cup {add}, !.qlOld = p.ql \ {add}]

QaVerdict(i, v, lab) ==
    pr' = [pr EXCEPT ![i] =
              SwapQ([@ EXCEPT !.qv = v, !.qAt = TRUE, !.qResp = FALSE, !.qNew = FALSE], lab)]

QaReview(i) ==
    /\ QaRuns /\ IsOpen(i) /\ QaStatus(i) = "review" /\ pr[i].qlOld = {}
    /\ \/ QaVerdict(i, "accepted", "accepted") /\ UNCHANGED bud
       \/ "qaFinding" \in bud /\ QaVerdict(i, "cr", "cr") /\ bud' = bud \ {"qaFinding"}
       \/ "qaHuman" \in bud /\ QaVerdict(i, "nh", "nh") /\ bud' = bud \ {"qaHuman"}
    /\ UNCHANGED <<iss, wk, baseOk, ghost>>

QaLabels2(i) ==
    /\ IsOpen(i) /\ pr[i].qlOld # {}
    /\ pr' = [pr EXCEPT ![i].ql = @ \ pr[i].qlOld, ![i].qlOld = {}]
    /\ UNCHANGED <<iss, wk, bud, baseOk, ghost>>

----------------------------------------------------------------------------
(* pm: close issues judged complete (issue_close.py:66-72 links an issue *)
(* by branch suffix, so a merged partial PR closes it)                     *)

PmClose(i) ==
    /\ iss[i].open /\ pr[i].st = "merged" /\ pr[i].partial
    /\ ~Fix("partialStaysOpen")
    /\ iss' = [iss EXCEPT ![i].open = FALSE]
    /\ ghost' = ghost \cup {"closedIncomplete"}
    /\ UNCHANGED <<pr, wk, bud, baseOk>>

----------------------------------------------------------------------------
(* Owner *)

OwnerApprove(i) ==
    /\ iss[i].open /\ ~iss[i].approved /\ "rejected" \notin iss[i].vl
    /\ iss' = [iss EXCEPT ![i].approved = TRUE]
    /\ UNCHANGED <<pr, wk, bud, baseOk, ghost>>

\* An answer ends needsUser. It also touches the item, so `skipped` expires.
OwnerAnswer(i) ==
    /\ iss[i].open /\ iss[i].ask # {}
    /\ iss' = [iss EXCEPT ![i].ask = {}, ![i].skip = {}]
    /\ UNCHANGED <<pr, wk, bud, baseOk, ghost>>

OwnerUnescalate(i) ==
    /\ IsOpen(i) /\ "escalated" \in pr[i].gl /\ pr[i].gv = "escalate"
    \* gatekeeper_queue.py:148-163,514-517: rounds and older markers restart.
    /\ pr' = [pr EXCEPT ![i].gl = @ \ {"escalated"}, ![i].gv = "none",
                        ![i].gAt = FALSE, ![i].rounds = 0, ![i].resp = FALSE,
                        ![i].cleanAt = FALSE]
    /\ UNCHANGED <<iss, wk, bud, baseOk, ghost>>

\* The owner's answer to qa:needs-human is a plain PR comment.
OwnerAnswerQa(i) ==
    /\ IsOpen(i) /\ QaStatus(i) = "needs-human"
    /\ pr' = [pr EXCEPT ![i].qNew = TRUE, ![i].gNew = TRUE,
                        ![i].wNew = IF @ = "fix" THEN @ ELSE "info"]
    /\ UNCHANGED <<iss, wk, bud, baseOk, ghost>>

OwnerComment(i) ==
    /\ IsOpen(i) /\ "ownerComment" \in bud
    /\ \E k \in {"info", "fix"} :
          pr' = [pr EXCEPT ![i].gNew = TRUE, ![i].qNew = TRUE,
                           ![i].wNew = k, ![i].defect = @ \/ (k = "fix")]
    /\ bud' = bud \ {"ownerComment"}
    /\ UNCHANGED <<iss, wk, baseOk, ghost>>

Green(p) == p.ci = "pass" /\ p.gated \in {"na", "pass"}
Signed(p) == p.gv = "approve" /\ p.gAt /\ p.qv = "accepted" /\ p.qAt

\* qa.py:130-161 signoff(): labels + markers at head, not draft, no conflict.
MergeGate(p) ==
    /\ "approved" \in p.gl /\ "accepted" \in p.ql /\ ~p.conflict
    /\ MergeMode = "signoff" => Signed(p)
    /\ Fix("signoffStrict") => Green(p) /\ "escalated" \notin p.gl

OwnerCheck(i) ==
    /\ IsOpen(i) /\ pr[i].own # "fresh" /\ MergeGate(pr[i])
    /\ pr' = [pr EXCEPT ![i].own = "fresh"]
    /\ UNCHANGED <<iss, wk, bud, baseOk, ghost>>

OwnerMerge(i) ==
    LET p == pr[i] IN
    /\ IsOpen(i) /\ p.own # "no" /\ ~p.conflict
    /\ IF Fix("matchHead") /\ ~(p.own = "fresh" /\ MergeGate(p))
       THEN /\ pr' = [pr EXCEPT ![i].own = "no"]
            /\ UNCHANGED <<iss, ghost>>
       ELSE /\ pr' = [pr EXCEPT ![i] = [NoPR EXCEPT !.st = "merged", !.partial = p.partial]]
            /\ iss' = IF p.partial THEN iss ELSE [iss EXCEPT ![i].open = FALSE]
            /\ ghost' = ghost
                  \cup (IF Signed(p) THEN {} ELSE {"mergeNoSignoff"})
                  \cup (IF Green(p) THEN {} ELSE {"mergeRed"})
    /\ UNCHANGED <<wk, bud, baseOk>>

----------------------------------------------------------------------------
(* Environment: CI, base branch, blockers, time, crashes *)

CiDone(i) ==
    /\ IsOpen(i) /\ pr[i].ci = "pending"
    /\ \/ baseOk /\ pr' = [pr EXCEPT ![i].ci = "pass"] /\ UNCHANGED <<bud, baseOk>>
       \/ /\ "ciFail" \in bud
          /\ pr' = [pr EXCEPT ![i].ci = "fail"] /\ bud' = bud \ {"ciFail"}
          /\ UNCHANGED baseOk
       \/ /\ "baseBreak" \in bud
          /\ pr' = [pr EXCEPT ![i].ci = "base"] /\ bud' = bud \ {"baseBreak"}
          /\ baseOk' = FALSE
       \* A head pushed while the base is broken fails for the same outside cause.
       \/ /\ ~baseOk /\ pr' = [pr EXCEPT ![i].ci = "base"]
          /\ UNCHANGED <<bud, baseOk>>
    /\ UNCHANGED <<iss, wk, ghost>>

GatedDone(i) ==
    /\ IsOpen(i) /\ pr[i].gated = "pending"
    /\ pr' = [pr EXCEPT ![i].gated = "pass"]
    /\ UNCHANGED <<iss, wk, bud, baseOk, ghost>>

\* A cancelled run that a later successful run of the same check superseded.
Supersede(i) ==
    /\ IsOpen(i) /\ pr[i].ci = "pass" /\ "cancelled" \in bud
    /\ pr' = [pr EXCEPT ![i].cancelled = TRUE]
    /\ bud' = bud \ {"cancelled"}
    /\ UNCHANGED <<iss, wk, baseOk, ghost>>

\* Abstracts the gap/bug issue, its approval and its merge. A PR whose CI already
\* failed keeps the old result until somebody pushes.
BaseFixed ==
    /\ ~baseOk /\ baseOk' = TRUE
    /\ UNCHANGED <<iss, pr, wk, bud, ghost>>

Conflict(i) ==
    /\ IsOpen(i) /\ "conflict" \in bud
    /\ pr' = [pr EXCEPT ![i].conflict = TRUE]
    /\ bud' = bud \ {"conflict"}
    /\ UNCHANGED <<iss, wk, baseOk, ghost>>

BlockerClose(i) ==
    /\ iss[i].blocker
    /\ iss' = [iss EXCEPT ![i].blocker = FALSE]
    /\ UNCHANGED <<pr, wk, bud, baseOk, ghost>>

\* The claim TTL passes while its owner is slow.
Slow(i) ==
    /\ iss[i].claim # Free /\ ~iss[i].stale /\ "stale" \in bud
    /\ iss' = [iss EXCEPT ![i].stale = TRUE]
    /\ bud' = bud \ {"stale"}
    /\ UNCHANGED <<pr, wk, baseOk, ghost>>

\* An orphaned claim always becomes stale in the end.
Expire(i) ==
    /\ iss[i].claim # Free /\ ~iss[i].stale
    /\ ~(wk[iss[i].claim].ph = "impl" /\ wk[iss[i].claim].i = i)
    /\ iss' = [iss EXCEPT ![i].stale = TRUE]
    /\ UNCHANGED <<pr, wk, bud, baseOk, ghost>>

Crash(w) ==
    /\ "crash" \in bud /\ wk[w].ph # "idle"
    /\ wk' = [wk EXCEPT ![w] = Idle]
    /\ bud' = bud \ {"crash"}
    /\ UNCHANGED <<iss, pr, baseOk, ghost>>

----------------------------------------------------------------------------

LoopStep ==
    \/ \E w \in Workers : WorkerImpl(w) \/ ImplFail(w) \/ ImplAsk(w)
    \/ \E w \in Workers, i \in Issues : WorkerPR(w, i) \/ Pick(w, i)
    \/ \E i \in Issues : GkVerdictStep(i) \/ GkLabels2(i) \/ GkValidate(i)
    \/ \E i \in Issues : QaReview(i) \/ QaLabels2(i) \/ PmClose(i)

OwnerStep ==
    \E i \in Issues : \/ OwnerApprove(i) \/ OwnerAnswer(i) \/ OwnerUnescalate(i)
                      \/ OwnerAnswerQa(i) \/ OwnerComment(i)
                      \/ OwnerCheck(i) \/ OwnerMerge(i)

EnvStep ==
    \/ BaseFixed
    \/ \E w \in Workers : Crash(w)
    \/ \E i \in Issues : \/ CiDone(i) \/ GatedDone(i) \/ Supersede(i)
                         \/ Conflict(i) \/ BlockerClose(i) \/ Slow(i) \/ Expire(i)

Next == LoopStep \/ OwnerStep \/ EnvStep

(***************************************************************************)
(* Fairness. SF on the three loop steps whose enabledness can flicker      *)
(* (plan priority, review status, PM cascade): a loop that is enabled      *)
(* again and again does run again and again. WF elsewhere. Failures,       *)
(* crashes, owner comments and other one-shot events have no fairness.     *)
(* Spec NoMergeSpec drops the owner's merge: the worker skill says an open *)
(* PR waiting for a merge must not stop issue work.                        *)
(***************************************************************************)
WorkerTick(w, i) == WorkerPR(w, i) \/ Pick(w, i)

Fairness ==
    /\ \A w \in Workers : WF_vars(WorkerImpl(w))
    /\ \A w \in Workers, i \in Issues : SF_vars(WorkerTick(w, i))
    /\ \A i \in Issues :
          /\ SF_vars(GkVerdictStep(i)) /\ WF_vars(GkLabels2(i)) /\ WF_vars(GkValidate(i))
          /\ SF_vars(QaReview(i)) /\ WF_vars(QaLabels2(i)) /\ WF_vars(PmClose(i))
          /\ WF_vars(OwnerApprove(i)) /\ WF_vars(OwnerAnswer(i))
          /\ WF_vars(OwnerUnescalate(i)) /\ WF_vars(OwnerAnswerQa(i))
          /\ WF_vars(CiDone(i)) /\ WF_vars(GatedDone(i))
          /\ WF_vars(BlockerClose(i)) /\ WF_vars(Expire(i))
    /\ WF_vars(BaseFixed)

MergeFairness == \A i \in Issues : WF_vars(OwnerCheck(i)) /\ WF_vars(OwnerMerge(i))

Spec == Init /\ [][Next]_vars
LiveSpec == Spec /\ Fairness /\ MergeFairness
NoMergeSpec == Spec /\ Fairness

----------------------------------------------------------------------------
(* Safety properties *)

\* P2: no merge without gatekeeper approve and qa accept at the merged head.
MergedOnlyWithSignoffs == "mergeNoSignoff" \notin ghost
\* P2b: no merge while CI or a required check is not green.
MergedOnlyGreen == "mergeRed" \notin ghost
\* P2c: no approve while a required check has not passed.
ApproveOnlyWithRequiredChecks == "approveUngated" \notin ghost
\* P3: at most one verdict label per reviewer.
OneVerdictLabel ==
    /\ \A i \in Issues : Cardinality(pr[i].gl) <= 1 /\ Cardinality(pr[i].ql) <= 1
    /\ \A i \in Issues : Cardinality(iss[i].vl) <= 1
\* P3b: the same, once the pending second label write has happened.
OneVerdictLabelSettled ==
    \A i \in Issues : /\ pr[i].glOld = {} => Cardinality(pr[i].gl) <= 1
                      /\ pr[i].qlOld = {} => Cardinality(pr[i].ql) <= 1
\* P7: no two workers implement one issue at the same time. The claim itself is
\* one value per issue (TypeOK), so two claim holders cannot exist.
OneImplementer ==
    \A v, w \in Workers :
        (v # w /\ wk[v].ph = "impl" /\ wk[w].ph = "impl") => wk[v].i # wk[w].i
\* P7b: two workers can believe they own it, but only the claim holder can push.
OnlyOneMayPush ==
    \A i \in Issues :
        Cardinality({w \in Workers : wk[w].ph = "impl" /\ wk[w].i = i /\ iss[i].claim = w}) <= 1
\* P8: one issue, one open PR.
NoDuplicatePR == "dupPR" \notin ghost
\* P9: an item parked for the owner is not worked on by another slot.
ParkedStaysParked == "parkBreach" \notin ghost
\* P10: no cr / approve verdict after the round limit.
RoundLimit == "overRound" \notin ghost
\* P11: an issue is closed only when its work is complete.
ClosedOnlyWhenComplete == "closedIncomplete" \notin ghost

\* Sanity only (expected to be violated): a PR can reach "merged" at all.
NeverMerged == \A i \in Issues : pr[i].st # "merged"

----------------------------------------------------------------------------
(* "No silent stall" and "owner only in explicit states" *)

Live(i) == iss[i].open /\ "rejected" \notin iss[i].vl

\* A state that a label or a needsUser record shows to the owner.
ExplicitOwner(i) ==
    \/ iss[i].ask # {}
    \/ IsOpen(i) /\ ("escalated" \in pr[i].gl \/ QaStatus(i) = "needs-human")

\* The two routine owner steps of the current design.
RoutineOwner(i) ==
    \/ ~IsOpen(i) /\ ~iss[i].approved
    \/ IsOpen(i) /\ (MergeGate(pr[i]) \/ pr[i].own # "no")

\* Something other than the owner will still change issue i without a new event.
MachineCanAct(i) ==
    \/ ENABLED <<\E w \in Workers : Pick(w, i) \/ WorkerPR(w, i)>>_vars
    \/ \E w \in Workers : wk[w].i = i /\ wk[w].ph # "idle"
    \/ ENABLED <<GkVerdictStep(i) \/ GkLabels2(i) \/ GkValidate(i)>>_vars
    \/ ENABLED <<QaReview(i) \/ QaLabels2(i) \/ PmClose(i)>>_vars
    \/ ENABLED <<CiDone(i) \/ GatedDone(i) \/ BlockerClose(i) \/ Expire(i)>>_vars
    \/ ~baseOk /\ IsOpen(i) /\ pr[i].ci = "base"

\* P4: every live item can be moved by a loop or CI, or the owner is asked.
NoSilentStall ==
    \A i \in Issues : Live(i) =>
        MachineCanAct(i) \/ ExplicitOwner(i) \/ RoutineOwner(i)

\* P6: if only the owner can move an item, it is in an explicit owner state.
OwnerOnlyExplicit ==
    \A i \in Issues : Live(i) => MachineCanAct(i) \/ ExplicitOwner(i)

----------------------------------------------------------------------------
(* Liveness *)

\* P5: an approved, unblocked issue gets a PR or an owner question.
IssueProgress ==
    \A i \in Issues :
        (Live(i) /\ iss[i].approved /\ ~iss[i].blocker /\ pr[i].st = "none")
            ~> (pr[i].st # "none" \/ iss[i].ask # {} \/ ~Live(i))

\* P12: an open PR gets merged, or the owner is asked.
PRProgress ==
    \A i \in Issues : IsOpen(i) ~> (pr[i].st = "merged" \/ ExplicitOwner(i))

\* P13: every open PR gets a qa verdict at some head, or leaves the open state.
QaProgress ==
    \A i \in Issues : IsOpen(i) ~> (pr[i].qv # "none" \/ ~IsOpen(i))

\* P14: a PR at the round limit is either escalated (owner asked) or moves on.
EscalationHappens ==
    \A i \in Issues :
        (IsOpen(i) /\ GkStatus(i) = "escalate")
            ~> (GkStatus(i) # "escalate" \/ "escalated" \in pr[i].gl \/ ~IsOpen(i))

=============================================================================
