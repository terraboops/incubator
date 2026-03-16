--------------------------- MODULE pool_scheduler ---------------------------
\* TLA+ specification of the incubator pool scheduler.
\* Models: priority queue, cadence-aware scheduling, parallel groups,
\*         max_concurrent limits, and the full job lifecycle.

EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
    Workers,            \* Set of worker IDs
    Ideas               \* Set of idea IDs

\* Model values for agents — hardcoded for tractable checking
PipelineAgents == <<"ideation", "validation">>
BackgroundAgents == {"watcher"}
SerialGroup == {"ideation", "validation"}
ParallelGroup == {"watcher"}
MaxConcurrent(agent) == 1

VARIABLES
    queue,              \* Set of [role |-> agent, idea |-> idea_id] records
    wState,             \* Function: worker -> "idle" | "running"
    wJob,               \* Function: worker -> record or NullJob
    results,            \* Function: idea -> function: agent -> "pending"|"proceed"|"iterate"
    phase,              \* Function: idea -> "active"|"released"|"killed"
    cadenceDue,         \* Function: background_agent -> BOOLEAN
    runCount            \* Function: agent -> nat (currently running instances)

vars == <<queue, wState, wJob, results, phase, cadenceDue, runCount>>

\* Sentinel values
NullJob == [role |-> "NONE", idea |-> "NONE"]
AllIdeas == "__all__"

\* All agent names
AllAgents == {PipelineAgents[i] : i \in 1..Len(PipelineAgents)} \cup BackgroundAgents

------------------------------------------------------------------------
\* Helpers

PipelineAgentSet == {PipelineAgents[i] : i \in 1..Len(PipelineAgents)}

\* Next pipeline agent: first in sequence with result "pending" or "iterate"
NextAgent(idea) ==
    LET pendingIdx == {i \in 1..Len(PipelineAgents) :
            results[idea][PipelineAgents[i]] \in {"pending", "iterate"}}
    IN IF pendingIdx = {}
       THEN "NONE"
       ELSE PipelineAgents[CHOOSE i \in pendingIdx : \A j \in pendingIdx : i <= j]

PipelineDone(idea) ==
    \A i \in 1..Len(PipelineAgents) : results[idea][PipelineAgents[i]] = "proceed"

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

------------------------------------------------------------------------
\* Initial state
Init ==
    /\ queue = {}
    /\ wState = [w \in Workers |-> "idle"]
    /\ wJob = [w \in Workers |-> NullJob]
    /\ results = [i \in Ideas |->
                    [a \in AllAgents |-> "pending"]]
    /\ phase = [i \in Ideas |-> "active"]
    /\ cadenceDue = [a \in BackgroundAgents |-> FALSE]
    /\ runCount = [a \in AllAgents |-> 0]

------------------------------------------------------------------------
\* ACTIONS

\* Producer: enqueue next pipeline agent for an active idea
ProducePipeline(idea) ==
    LET agent == NextAgent(idea)
    IN /\ phase[idea] = "active"
       /\ agent # "NONE"
       /\ ~InQueue(agent, idea)
       /\ ~IsRunning(agent, idea)
       /\ queue' = queue \cup {[role |-> agent, idea |-> idea]}
       /\ UNCHANGED <<wState, wJob, results, phase, cadenceDue, runCount>>

\* Cadence fires for a background agent
CadenceTick(agent) ==
    /\ agent \in BackgroundAgents
    /\ ~cadenceDue[agent]
    /\ cadenceDue' = [cadenceDue EXCEPT ![agent] = TRUE]
    /\ UNCHANGED <<queue, wState, wJob, results, phase, runCount>>

\* Producer: enqueue a due background job
ProduceBackground(agent) ==
    /\ agent \in BackgroundAgents
    /\ cadenceDue[agent]
    /\ ~InQueue(agent, AllIdeas)
    /\ ~IsRunning(agent, AllIdeas)
    /\ queue' = queue \cup {[role |-> agent, idea |-> AllIdeas]}
    /\ UNCHANGED <<wState, wJob, results, phase, cadenceDue, runCount>>

\* Priority: pipeline jobs beat background jobs
JobPriority(job) ==
    IF job.role \in PipelineAgentSet THEN 2 ELSE 1

\* Dispatch: idle worker takes the HIGHEST PRIORITY schedulable job from queue
Dispatch(worker, job) ==
    /\ wState[worker] = "idle"
    /\ job \in queue
    /\ Schedulable(job.role, job.idea)
    \* Must pick the highest priority schedulable job
    /\ \A other \in queue :
        Schedulable(other.role, other.idea) => JobPriority(other) <= JobPriority(job)
    /\ queue' = queue \ {job}
    /\ wState' = [wState EXCEPT ![worker] = "running"]
    /\ wJob' = [wJob EXCEPT ![worker] = job]
    /\ runCount' = [runCount EXCEPT ![job.role] = @ + 1]
    /\ UNCHANGED <<results, phase, cadenceDue>>

\* Complete: pipeline agent finishes with "proceed"
\* If this was the last pipeline agent, auto-release the idea
CompleteProceed(worker) ==
    LET idea == wJob[worker].idea
        agent == wJob[worker].role
        newResults == [results EXCEPT ![idea][agent] = "proceed"]
        allDone == \A i \in 1..Len(PipelineAgents) :
                        newResults[idea][PipelineAgents[i]] = "proceed"
    IN /\ wState[worker] = "running"
       /\ wJob[worker] # NullJob
       /\ idea # AllIdeas
       /\ agent \in PipelineAgentSet
       /\ results' = newResults
       /\ phase' = IF allDone THEN [phase EXCEPT ![idea] = "released"] ELSE phase
       /\ wState' = [wState EXCEPT ![worker] = "idle"]
       /\ runCount' = [runCount EXCEPT ![agent] = @ - 1]
       /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
       /\ UNCHANGED <<queue, cadenceDue>>

\* Complete: pipeline agent finishes with "iterate"
CompleteIterate(worker) ==
    /\ wState[worker] = "running"
    /\ wJob[worker] # NullJob
    /\ wJob[worker].idea # AllIdeas
    /\ wJob[worker].role \in PipelineAgentSet
    /\ results' = [results EXCEPT ![wJob[worker].idea][wJob[worker].role] = "iterate"]
    /\ wState' = [wState EXCEPT ![worker] = "idle"]
    /\ runCount' = [runCount EXCEPT ![wJob[worker].role] = @ - 1]
    /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
    /\ UNCHANGED <<queue, phase, cadenceDue>>

\* Complete: background agent finishes
CompleteBack(worker) ==
    /\ wState[worker] = "running"
    /\ wJob[worker] # NullJob
    /\ wJob[worker].idea = AllIdeas
    /\ wJob[worker].role \in BackgroundAgents
    /\ cadenceDue' = [cadenceDue EXCEPT ![wJob[worker].role] = FALSE]
    /\ wState' = [wState EXCEPT ![worker] = "idle"]
    /\ runCount' = [runCount EXCEPT ![wJob[worker].role] = @ - 1]
    /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
    /\ UNCHANGED <<queue, results, phase>>

\* Complete: error (any agent) — frees worker, doesn't change results
\* Background agents reset cadence on error (they "tried" — will retry next cadence tick)
CompleteError(worker) ==
    /\ wState[worker] = "running"
    /\ wJob[worker] # NullJob
    /\ wState' = [wState EXCEPT ![worker] = "idle"]
    /\ runCount' = [runCount EXCEPT ![wJob[worker].role] = @ - 1]
    /\ cadenceDue' = IF wJob[worker].role \in BackgroundAgents
                     THEN [cadenceDue EXCEPT ![wJob[worker].role] = FALSE]
                     ELSE cadenceDue
    /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
    /\ UNCHANGED <<queue, results, phase>>

\* Agent kills idea
Kill(worker) ==
    /\ wState[worker] = "running"
    /\ wJob[worker] # NullJob
    /\ wJob[worker].idea # AllIdeas
    /\ phase' = [phase EXCEPT ![wJob[worker].idea] = "killed"]
    /\ wState' = [wState EXCEPT ![worker] = "idle"]
    /\ runCount' = [runCount EXCEPT ![wJob[worker].role] = @ - 1]
    /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
    /\ UNCHANGED <<queue, results, cadenceDue>>

------------------------------------------------------------------------
Next ==
    \/ \E i \in Ideas : ProducePipeline(i)
    \/ \E a \in BackgroundAgents : CadenceTick(a)
    \/ \E a \in BackgroundAgents : ProduceBackground(a)
    \/ \E w \in Workers, j \in queue : Dispatch(w, j)
    \/ \E w \in Workers : CompleteProceed(w)
    \/ \E w \in Workers : CompleteIterate(w)
    \/ \E w \in Workers : CompleteBack(w)
    \/ \E w \in Workers : CompleteError(w)
    \/ \E w \in Workers : Kill(w)

\* Strong fairness: if an action is repeatedly enabled, it must eventually fire.
\* This models the implementation where all producers run every loop iteration.
Fairness ==
    /\ \A i \in Ideas : SF_vars(ProducePipeline(i))
    /\ \A a \in BackgroundAgents : SF_vars(ProduceBackground(a))
    /\ \A a \in BackgroundAgents : SF_vars(CadenceTick(a))
    /\ \A w \in Workers : SF_vars(\E j \in queue : Dispatch(w, j))
    /\ \A w \in Workers : SF_vars(CompleteProceed(w))
    /\ \A w \in Workers : SF_vars(CompleteIterate(w))
    /\ \A w \in Workers : SF_vars(CompleteBack(w))
    /\ \A w \in Workers : SF_vars(CompleteError(w))
    /\ \A w \in Workers : SF_vars(Kill(w))

Spec == Init /\ [][Next]_vars /\ Fairness

------------------------------------------------------------------------
\* SAFETY INVARIANTS

MaxConcurrentOK ==
    \A a \in AllAgents : runCount[a] <= MaxConcurrent(a)

SerialGroupOK ==
    \A w1, w2 \in Workers :
        (w1 # w2
         /\ wState[w1] = "running" /\ wState[w2] = "running"
         /\ wJob[w1] # NullJob /\ wJob[w2] # NullJob
         /\ wJob[w1].idea = wJob[w2].idea
         /\ wJob[w1].idea # AllIdeas)
        => ~(wJob[w1].role \in SerialGroup /\ wJob[w2].role \in SerialGroup)

RunCountOK ==
    \A a \in AllAgents :
        runCount[a] = Cardinality({w \in Workers :
            wState[w] = "running" /\ wJob[w] # NullJob /\ wJob[w].role = a})

\* Note: NoWorkOnDead is not an invariant because kill can race with enqueue.
\* The implementation handles this by checking phase in _handle_result.
NoWorkOnDead ==
    \A w \in Workers :
        (wState[w] = "running" /\ wJob[w] # NullJob /\ wJob[w].idea # AllIdeas)
        => phase[wJob[w].idea] # "killed"

------------------------------------------------------------------------
\* LIVENESS PROPERTIES

\* Every active idea eventually completes
Progress == \A i \in Ideas :
    phase[i] = "active" ~> phase[i] \in {"released", "killed"}

\* Every due watcher eventually runs
WatcherProgress == \A a \in BackgroundAgents :
    cadenceDue[a] = TRUE ~> cadenceDue[a] = FALSE

========================================================================
