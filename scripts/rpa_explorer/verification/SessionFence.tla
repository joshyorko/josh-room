----------------------------- MODULE SessionFence -----------------------------
EXTENDS Naturals
CONSTANT EnforceFence
VARIABLES phase, generation, observation, closed, requestGeneration,
          requestObservation, requestIdentity, badDispatch
vars == <<phase, generation, observation, closed, requestGeneration,
          requestObservation, requestIdentity, badDispatch>>

Init == /\ phase = "ready" /\ generation = 1 /\ observation = 0
        /\ closed = FALSE /\ requestGeneration = 1 /\ requestObservation = 0
        /\ requestIdentity = "same" /\ badDispatch = FALSE

Queue == /\ phase = "ready"
         /\ phase' = "pending"
         /\ requestGeneration' = generation
         /\ requestObservation' = observation
         /\ requestIdentity' \in {"same", "other-session", "other-candidate", "other-attempts"}
         /\ UNCHANGED <<generation, observation, closed, badDispatch>>

Observe == /\ ~closed /\ observation = 0
           /\ observation' = 1
           /\ UNCHANGED <<phase, generation, closed, requestGeneration,
                          requestObservation, requestIdentity, badDispatch>>

Close == /\ ~closed
         /\ closed' = TRUE /\ generation' = 2
         /\ UNCHANGED <<phase, observation, requestGeneration,
                        requestObservation, requestIdentity, badDispatch>>

Current == ~closed /\ requestGeneration = generation
           /\ requestObservation = observation /\ requestIdentity = "same"

Dispatch == /\ phase = "pending"
            /\ phase' = IF ~EnforceFence \/ Current THEN "dispatched" ELSE "rejected"
            /\ badDispatch' = ((~EnforceFence \/ Current) /\ ~Current)
            /\ UNCHANGED <<generation, observation, closed, requestGeneration,
                           requestObservation, requestIdentity>>

Done == /\ phase \in {"dispatched", "rejected"}
        /\ UNCHANGED vars
Next == Queue \/ Observe \/ Close \/ Dispatch \/ Done
Spec == Init /\ [][Next]_vars

TypeOK == /\ phase \in {"ready", "pending", "dispatched", "rejected"}
          /\ generation \in {1, 2} /\ observation \in {0, 1}
          /\ closed \in BOOLEAN /\ badDispatch \in BOOLEAN
NoStaleDispatch == ~badDispatch
NotYetDispatched == phase # "dispatched"
=============================================================================
