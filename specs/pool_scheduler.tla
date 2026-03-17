--------------------------- MODULE pool_scheduler ---------------------------
\* TLA+ specification of the incubator pool scheduler.
\* Models: priority queue, cadence-aware scheduling, parallel groups,
\*         gating modes, feedback scheduling, refinement loops,
\*         iteration caps, and per-idea background agents.
\*
\* Updated 2026-03-16 to match current implementation.

EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
    Workers,            \* Set of worker IDs
    Ideas               \* Set of idea IDs

\* Model values — hardcoded for tractable checking
PipelineAgents == <<"ideation", "validation">>
BackgroundAgents == {"watcher"}
AllAgentNames == {PipelineAgents[i] : i \in 1..Len(PipelineAgents)} \cup BackgroundAgents

\* Parallel groups: agents in the same group serialize on the same idea
SerialGroup == {"ideation", "validation"}
MaxConcurrent(agent) == 1

\* Gating modes: "auto" | "human" | "llm"
\* For model checking, each idea gets a fixed mode
GatingMode == "auto"

\* Refinement: how many times pipeline can loop before terminal release
MaxRefinementCycles == 1
MaxIteratePerStage == 3

VARIABLES
    queue,              \* Set of [role, idea, kind, priority] records
    wState,             \* Function: worker -> "idle" | "running"
    wJob,               \* Function: worker -> record or NullJob
    phase,              \* Function: idea -> phase string
    stageResults,       \* Function: idea -> function: agent -> "pending"|"proceed"|"iterate"
    iterCount,          \* Function: idea -> function: agent -> nat (per-stage iteration count)
    releaseCount,       \* Function: idea -> nat (completed pipeline passes)
    needsReview,        \* Function: idea -> BOOLEAN (human review gate)
    hasFeedback,        \* Function: idea -> function: agent -> BOOLEAN
    cadenceDue,         \* Function: background_agent -> BOOLEAN
    runCount            \* Function: agent -> nat (currently running instances)

vars == <<queue, wState, wJob, phase, stageResults, iterCount,
          releaseCount, needsReview, hasFeedback, cadenceDue, runCount>>

\* Sentinel values
NullJob == [role |-> "NONE", idea |-> "NONE", kind |-> "NONE", priority |-> 0]

------------------------------------------------------------------------
\* Helpers

PipelineAgentSet == {PipelineAgents[i] : i \in 1..Len(PipelineAgents)}

\* Next pipeline agent: first in sequence with result "pending" or "iterate"
NextAgent(idea) ==
    LET pendingIdx == {i \in 1..Len(PipelineAgents) :
            stageResults[idea][PipelineAgents[i]] \in {"pending", "iterate"}}
    IN IF pendingIdx = {}
       THEN "NONE"
       ELSE PipelineAgents[CHOOSE i \in pendingIdx : \A j \in pendingIdx : i <= j]

PipelineDone(idea) ==
    \A i \in 1..Len(PipelineAgents) : stageResults[idea][PipelineAgents[i]] = "proceed"

\* Group conflict: another agent from the serial group is running on this idea
HasConflict(agent, idea) ==
    /\ agent \in SerialGroup
    /\ \E w \in Workers :
        /\ wState[w] = "running"
        /\ wJob[w] # NullJob
        /\ wJob[w].idea = idea
        /\ wJob[w].role \in SerialGroup
        /\ wJob[w].role # agent

IsRunning(agent, idea) ==
    \E w \in Workers :
        /\ wState[w] = "running"
        /\ wJob[w] # NullJob
        /\ wJob[w].role = agent
        /\ wJob[w].idea = idea

InQueue(agent, idea) ==
    \E j \in queue : j.role = agent /\ j.idea = idea

Schedulable(agent, idea) ==
    /\ runCount[agent] < MaxConcurrent(agent)
    /\ ~HasConflict(agent, idea)
    /\ ~IsRunning(agent, idea)

\* Phases where pipeline work is eligible
IsEligible(idea) ==
    /\ phase[idea] \notin {"killed", "paused", "released"}
    /\ ~needsReview[idea]

\* Priority: pipeline=5, feedback=4, background=3 (simplified from continuous floats)
JobPriority(job) ==
    IF job.kind = "pipeline" THEN 5
    ELSE IF job.kind = "feedback" THEN 4
    ELSE 3  \* background

------------------------------------------------------------------------
\* Initial state
Init ==
    /\ queue = {}
    /\ wState = [w \in Workers |-> "idle"]
    /\ wJob = [w \in Workers |-> NullJob]
    /\ phase = [i \in Ideas |-> "submitted"]
    /\ stageResults = [i \in Ideas |->
                         [a \in AllAgentNames |-> "pending"]]
    /\ iterCount = [i \in Ideas |->
                      [a \in PipelineAgentSet |-> 0]]
    /\ releaseCount = [i \in Ideas |-> 0]
    /\ needsReview = [i \in Ideas |-> FALSE]
    /\ hasFeedback = [i \in Ideas |->
                        [a \in AllAgentNames |-> FALSE]]
    /\ cadenceDue = [a \in BackgroundAgents |-> FALSE]
    /\ runCount = [a \in AllAgentNames |-> 0]

------------------------------------------------------------------------
\* ACTIONS

\* ── Producers ────────────────────────────────────────────────────

\* Producer: enqueue next pipeline agent for an eligible idea
ProducePipeline(idea) ==
    LET agent == NextAgent(idea)
    IN /\ IsEligible(idea)
       /\ ~PipelineDone(idea)
       /\ agent # "NONE"
       /\ ~InQueue(agent, idea)
       /\ ~IsRunning(agent, idea)
       /\ queue' = queue \cup {[role |-> agent, idea |-> idea,
                                 kind |-> "pipeline", priority |-> 5]}
       /\ UNCHANGED <<wState, wJob, phase, stageResults, iterCount,
                       releaseCount, needsReview, hasFeedback, cadenceDue, runCount>>

\* Producer: enqueue feedback-driven work for an agent with pending feedback
ProduceFeedback(idea, agent) ==
    /\ IsEligible(idea)
    /\ agent \in PipelineAgentSet
    /\ hasFeedback[idea][agent]
    /\ ~InQueue(agent, idea)
    /\ ~IsRunning(agent, idea)
    /\ queue' = queue \cup {[role |-> agent, idea |-> idea,
                              kind |-> "feedback", priority |-> 4]}
    /\ UNCHANGED <<wState, wJob, phase, stageResults, iterCount,
                    releaseCount, needsReview, hasFeedback, cadenceDue, runCount>>

\* Cadence fires for a background agent
CadenceTick(agent) ==
    /\ agent \in BackgroundAgents
    /\ ~cadenceDue[agent]
    /\ cadenceDue' = [cadenceDue EXCEPT ![agent] = TRUE]
    /\ UNCHANGED <<queue, wState, wJob, phase, stageResults, iterCount,
                    releaseCount, needsReview, hasFeedback, runCount>>

\* Producer: enqueue a due background agent PER IDEA (not global)
ProduceBackground(agent, idea) ==
    /\ agent \in BackgroundAgents
    /\ cadenceDue[agent]
    /\ phase[idea] # "killed"  \* background agents run on all non-killed ideas
    /\ ~InQueue(agent, idea)
    /\ ~IsRunning(agent, idea)
    /\ queue' = queue \cup {[role |-> agent, idea |-> idea,
                              kind |-> "background", priority |-> 3]}
    /\ UNCHANGED <<wState, wJob, phase, stageResults, iterCount,
                    releaseCount, needsReview, hasFeedback, cadenceDue, runCount>>

\* External: human submits feedback targeting an agent on an idea
SubmitFeedback(idea, agent) ==
    /\ phase[idea] # "killed"
    /\ agent \in PipelineAgentSet
    /\ ~hasFeedback[idea][agent]
    /\ hasFeedback' = [hasFeedback EXCEPT ![idea][agent] = TRUE]
    /\ UNCHANGED <<queue, wState, wJob, phase, stageResults, iterCount,
                    releaseCount, needsReview, cadenceDue, runCount>>

\* External: human dismisses review gate — resets iteration counters
\* (human intervention = fresh mandate, agent gets new attempts)
DismissReview(idea) ==
    /\ needsReview[idea]
    /\ needsReview' = [needsReview EXCEPT ![idea] = FALSE]
    /\ iterCount' = [iterCount EXCEPT ![idea] =
                       [a \in PipelineAgentSet |-> 0]]
    /\ UNCHANGED <<queue, wState, wJob, phase, stageResults,
                    releaseCount, hasFeedback, cadenceDue, runCount>>

\* ── Dispatch ─────────────────────────────────────────────────────

\* Dispatch: idle worker takes the HIGHEST PRIORITY schedulable job
Dispatch(worker, job) ==
    /\ wState[worker] = "idle"
    /\ job \in queue
    /\ Schedulable(job.role, job.idea)
    \* Must pick the highest priority schedulable job
    /\ \A other \in queue :
        Schedulable(other.role, other.idea) => other.priority <= job.priority
    /\ queue' = queue \ {job}
    /\ wState' = [wState EXCEPT ![worker] = "running"]
    /\ wJob' = [wJob EXCEPT ![worker] = job]
    /\ runCount' = [runCount EXCEPT ![job.role] = @ + 1]
    /\ UNCHANGED <<phase, stageResults, iterCount, releaseCount,
                    needsReview, hasFeedback, cadenceDue>>

\* ── Completion: pipeline agent proceeds ──────────────────────────

CompleteProceed(worker) ==
    LET idea == wJob[worker].idea
        agent == wJob[worker].role
        newResults == [stageResults EXCEPT ![idea][agent] = "proceed"]
        allDone == \A i \in 1..Len(PipelineAgents) :
                        newResults[idea][PipelineAgents[i]] = "proceed"
        \* Refinement: if all done and under max cycles, loop back
        shouldLoop == allDone /\ releaseCount[idea] < MaxRefinementCycles
        shouldRelease == allDone /\ releaseCount[idea] >= MaxRefinementCycles
    IN /\ wState[worker] = "running"
       /\ wJob[worker] # NullJob
       /\ wJob[worker].kind \in {"pipeline", "feedback"}
       /\ agent \in PipelineAgentSet
       \* Gating: human-review mode blocks before proceeding
       /\ IF GatingMode = "human"
          THEN /\ needsReview' = [needsReview EXCEPT ![idea] = TRUE]
               /\ stageResults' = newResults
               /\ phase' = phase
               /\ releaseCount' = releaseCount
               /\ iterCount' = iterCount
          ELSE /\ stageResults' = IF shouldLoop
                                  THEN [newResults EXCEPT ![idea] =
                                        [a \in AllAgentNames |-> "pending"]]
                                  ELSE newResults
               /\ phase' = IF shouldRelease
                            THEN [phase EXCEPT ![idea] = "released"]
                            ELSE phase
               /\ releaseCount' = IF allDone
                                  THEN [releaseCount EXCEPT ![idea] = @ + 1]
                                  ELSE releaseCount
               /\ iterCount' = [iterCount EXCEPT ![idea][agent] = 0]
               /\ needsReview' = needsReview
       /\ wState' = [wState EXCEPT ![worker] = "idle"]
       /\ runCount' = [runCount EXCEPT ![agent] = @ - 1]
       /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
       /\ UNCHANGED <<queue, hasFeedback, cadenceDue>>

\* ── Completion: pipeline agent iterates ──────────────────────────

CompleteIterate(worker) ==
    LET idea == wJob[worker].idea
        agent == wJob[worker].role
        iters == iterCount[idea][agent] + 1
        hitCap == iters >= MaxIteratePerStage
    IN /\ wState[worker] = "running"
       /\ wJob[worker] # NullJob
       /\ wJob[worker].kind \in {"pipeline", "feedback"}
       /\ agent \in PipelineAgentSet
       /\ stageResults' = [stageResults EXCEPT ![idea][agent] = "iterate"]
       /\ iterCount' = [iterCount EXCEPT ![idea][agent] = iters]
       \* If hit iteration cap, gate to human review instead of looping
       /\ needsReview' = IF hitCap
                          THEN [needsReview EXCEPT ![idea] = TRUE]
                          ELSE needsReview
       /\ wState' = [wState EXCEPT ![worker] = "idle"]
       /\ runCount' = [runCount EXCEPT ![agent] = @ - 1]
       /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
       /\ UNCHANGED <<queue, phase, releaseCount, hasFeedback, cadenceDue>>

\* ── Completion: background agent finishes ────────────────────────

CompleteBack(worker) ==
    /\ wState[worker] = "running"
    /\ wJob[worker] # NullJob
    /\ wJob[worker].kind = "background"
    /\ wJob[worker].role \in BackgroundAgents
    /\ cadenceDue' = [cadenceDue EXCEPT ![wJob[worker].role] = FALSE]
    /\ wState' = [wState EXCEPT ![worker] = "idle"]
    /\ runCount' = [runCount EXCEPT ![wJob[worker].role] = @ - 1]
    /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
    /\ UNCHANGED <<queue, phase, stageResults, iterCount, releaseCount,
                    needsReview, hasFeedback>>

\* ── Completion: feedback run clears the feedback flag ─────────────

CompleteFeedbackRun(worker) ==
    LET idea == wJob[worker].idea
        agent == wJob[worker].role
    IN /\ wState[worker] = "running"
       /\ wJob[worker] # NullJob
       /\ wJob[worker].kind = "feedback"
       /\ agent \in PipelineAgentSet
       /\ hasFeedback' = [hasFeedback EXCEPT ![idea][agent] = FALSE]
       /\ wState' = [wState EXCEPT ![worker] = "idle"]
       /\ runCount' = [runCount EXCEPT ![agent] = @ - 1]
       /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
       /\ UNCHANGED <<queue, phase, stageResults, iterCount, releaseCount,
                       needsReview, cadenceDue>>

\* ── Completion: error (any agent) ────────────────────────────────
\* TLA+ verified finding: background agents MUST reset cadence on error
\* to prevent livelock (perpetually "due" → infinite retry loop)

CompleteError(worker) ==
    /\ wState[worker] = "running"
    /\ wJob[worker] # NullJob
    /\ wState' = [wState EXCEPT ![worker] = "idle"]
    /\ runCount' = [runCount EXCEPT ![wJob[worker].role] = @ - 1]
    /\ cadenceDue' = IF wJob[worker].role \in BackgroundAgents
                     THEN [cadenceDue EXCEPT ![wJob[worker].role] = FALSE]
                     ELSE cadenceDue
    /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
    /\ UNCHANGED <<queue, phase, stageResults, iterCount, releaseCount,
                    needsReview, hasFeedback>>

\* ── Agent kills idea ─────────────────────────────────────────────
\* Kill also drains the queue of work for this idea (the real code
\* handles this lazily in _handle_result, but modeled atomically here)

Kill(worker) ==
    LET idea == wJob[worker].idea
    IN /\ wState[worker] = "running"
       /\ wJob[worker] # NullJob
       /\ wJob[worker].role \in PipelineAgentSet
       /\ phase' = [phase EXCEPT ![idea] = "killed"]
       /\ queue' = {j \in queue : j.idea # idea}
       /\ wState' = [wState EXCEPT ![worker] = "idle"]
       /\ runCount' = [runCount EXCEPT ![wJob[worker].role] = @ - 1]
       /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
       /\ UNCHANGED <<stageResults, iterCount, releaseCount,
                       needsReview, hasFeedback, cadenceDue>>

------------------------------------------------------------------------
Next ==
    \/ \E i \in Ideas : ProducePipeline(i)
    \/ \E i \in Ideas, a \in PipelineAgentSet : ProduceFeedback(i, a)
    \/ \E a \in BackgroundAgents : CadenceTick(a)
    \/ \E a \in BackgroundAgents, i \in Ideas : ProduceBackground(a, i)
    \/ \E i \in Ideas, a \in PipelineAgentSet : SubmitFeedback(i, a)
    \/ \E i \in Ideas : DismissReview(i)
    \/ \E w \in Workers, j \in queue : Dispatch(w, j)
    \/ \E w \in Workers : CompleteProceed(w)
    \/ \E w \in Workers : CompleteIterate(w)
    \/ \E w \in Workers : CompleteBack(w)
    \/ \E w \in Workers : CompleteFeedbackRun(w)
    \/ \E w \in Workers : CompleteError(w)
    \/ \E w \in Workers : Kill(w)

\* Strong fairness: if an action is repeatedly enabled, it must eventually fire.
Fairness ==
    /\ \A i \in Ideas : SF_vars(ProducePipeline(i))
    /\ \A i \in Ideas, a \in PipelineAgentSet : SF_vars(ProduceFeedback(i, a))
    /\ \A a \in BackgroundAgents : SF_vars(CadenceTick(a))
    /\ \A a \in BackgroundAgents, i \in Ideas : SF_vars(ProduceBackground(a, i))
    /\ \A i \in Ideas : SF_vars(DismissReview(i))
    /\ \A w \in Workers : SF_vars(\E j \in queue : Dispatch(w, j))
    /\ \A w \in Workers : SF_vars(CompleteProceed(w))
    /\ \A w \in Workers : SF_vars(CompleteIterate(w))
    /\ \A w \in Workers : SF_vars(CompleteBack(w))
    /\ \A w \in Workers : SF_vars(CompleteFeedbackRun(w))
    /\ \A w \in Workers : SF_vars(CompleteError(w))
    /\ \A w \in Workers : SF_vars(Kill(w))

Spec == Init /\ [][Next]_vars /\ Fairness

------------------------------------------------------------------------
\* SAFETY INVARIANTS

\* No agent exceeds max_concurrent running instances
MaxConcurrentOK ==
    \A a \in AllAgentNames : runCount[a] <= MaxConcurrent(a)

\* Serial group agents never run simultaneously on the same idea
SerialGroupOK ==
    \A w1, w2 \in Workers :
        (w1 # w2
         /\ wState[w1] = "running" /\ wState[w2] = "running"
         /\ wJob[w1] # NullJob /\ wJob[w2] # NullJob
         /\ wJob[w1].idea = wJob[w2].idea)
        => ~(wJob[w1].role \in SerialGroup /\ wJob[w2].role \in SerialGroup)

\* runCount accurately reflects the number of running instances
RunCountOK ==
    \A a \in AllAgentNames :
        runCount[a] = Cardinality({w \in Workers :
            wState[w] = "running" /\ wJob[w] # NullJob /\ wJob[w].role = a})

\* Iteration count never exceeds cap + 1 (cap is checked after increment)
IterCountBounded ==
    \A i \in Ideas, a \in PipelineAgentSet :
        iterCount[i][a] <= MaxIteratePerStage

\* Released ideas are terminal (no more pipeline work)
ReleasedIsTerminal ==
    \A i \in Ideas :
        phase[i] = "released" =>
            \A w \in Workers :
                (wState[w] = "running" /\ wJob[w].idea = i)
                => wJob[w].kind = "background"

\* Killed ideas get no new pipeline or feedback work
\* (Note: in-progress work can race — checked post-completion in code)
NoNewWorkOnKilled ==
    \A j \in queue :
        j.kind \in {"pipeline", "feedback"} => phase[j.idea] # "killed"

------------------------------------------------------------------------
\* LIVENESS PROPERTIES

\* Every submitted idea eventually reaches released or killed
\* (requires DismissReview fairness to unblock gated ideas)
Progress == \A i \in Ideas :
    phase[i] = "submitted" ~> phase[i] \in {"released", "killed"}

\* Every due watcher eventually runs — unless all ideas are killed/released
\* (no work to enqueue if every idea is terminal)
WatcherProgress == \A a \in BackgroundAgents :
    (cadenceDue[a] = TRUE /\ \E i \in Ideas : phase[i] \notin {"killed", "released"})
    ~> cadenceDue[a] = FALSE

\* Feedback is eventually consumed — unless the idea is killed
FeedbackProgress == \A i \in Ideas, a \in PipelineAgentSet :
    (hasFeedback[i][a] = TRUE /\ phase[i] # "killed")
    ~> (hasFeedback[i][a] = FALSE \/ phase[i] = "killed")

\* Review gates are eventually dismissed (by human action)
ReviewProgress == \A i \in Ideas :
    needsReview[i] = TRUE ~> needsReview[i] = FALSE

========================================================================
