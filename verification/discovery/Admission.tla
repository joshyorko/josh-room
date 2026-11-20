--------------------------- MODULE Admission ---------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS Names, BrokenAdmission, PrioritizeFinalization, BrokenOffer
VARIABLES entries, directories, seen, pending, rootState, opened, offered, paused
vars == <<entries, directories, seen, pending, rootState, opened, offered, paused>>

(* Local sibling scope: each name may be a directory or a regular file.
   "safe" and "SAFE" collide; "other" has a distinct alias. *)
Alias(n) == IF n = "SAFE" THEN "safe" ELSE n
Unique(n) == Cardinality({m \in entries : Alias(m) = Alias(n)}) = 1

Init == /\ entries \in SUBSET Names
        /\ directories \in SUBSET entries
        /\ seen = {}
        /\ pending = [n \in Names |-> "absent"]
        /\ rootState = "reading"
        /\ opened = {}
        /\ offered = {}
        /\ paused = FALSE

ReadEntry == /\ ~paused /\ rootState = "reading"
             /\ \E n \in entries \ seen :
                 /\ seen' = seen \cup {n}
                 /\ pending' = IF n \in directories
                       THEN [pending EXCEPT ![n] = IF BrokenAdmission THEN "queued" ELSE "staged"]
                       ELSE pending
             /\ UNCHANGED <<entries, directories, rootState, opened, offered, paused>>

EndRead == /\ ~paused /\ rootState = "reading" /\ seen = entries
           /\ rootState' = "finalizing"
           /\ UNCHANGED <<entries, directories, seen, pending, opened, offered, paused>>

ReadFailure == /\ ~paused /\ rootState = "reading"
               /\ rootState' = "failed"
               /\ UNCHANGED <<entries, directories, seen, pending, opened, offered, paused>>

FinalizeChild == /\ ~paused /\ rootState = "finalizing"
                 /\ \E n \in Names :
                     /\ pending[n] = "staged"
                     /\ pending' = [pending EXCEPT ![n] = IF Unique(n) THEN "queued" ELSE "rejected"]
                 /\ UNCHANGED <<entries, directories, seen, rootState, opened, offered, paused>>

FinishParent == /\ ~paused /\ rootState = "finalizing"
                /\ ~\E n \in Names : pending[n] = "staged"
                /\ rootState' = "validated"
                /\ UNCHANGED <<entries, directories, seen, pending, opened, offered, paused>>

(* The implementation does not check parent state at _open: staging and
   queue priority must establish it. Do not add that missing guard here. *)
OpenChild == /\ ~paused /\ rootState # "reading"
             /\ ~(PrioritizeFinalization /\ rootState = "finalizing")
             /\ \E n \in Names :
                 /\ pending[n] = "queued"
                 /\ opened' = opened \cup {n}
                 /\ pending' = [pending EXCEPT ![n] = "opened"]
             /\ UNCHANGED <<entries, directories, seen, rootState, offered, paused>>

OfferChild == /\ ~paused /\ rootState \in {"failed", "validated"}
              /\ ~\E n \in Names : pending[n] = "queued"
              /\ offered' = {n \in Names : IF BrokenOffer
                    THEN pending[n] \in {"staged", "queued", "opened"}
                    ELSE pending[n] = "opened"}
              /\ UNCHANGED <<entries, directories, seen, pending, rootState, opened, paused>>

Pause == /\ ~paused /\ paused' = TRUE
         /\ UNCHANGED <<entries, directories, seen, pending, rootState, opened, offered>>
Resume == /\ paused /\ paused' = FALSE
          /\ UNCHANGED <<entries, directories, seen, pending, rootState, opened, offered>>

Next == ReadEntry \/ EndRead \/ ReadFailure \/ FinalizeChild \/ FinishParent
        \/ OpenChild \/ OfferChild \/ Pause \/ Resume

SiblingAdmission == \A n \in opened \cup offered :
    /\ rootState = "validated"
    /\ seen = entries
    /\ Unique(n)

TypeOK == /\ entries \subseteq Names /\ directories \subseteq entries /\ seen \subseteq Names
          /\ opened \subseteq directories /\ offered \subseteq directories
          /\ rootState \in {"reading", "finalizing", "validated", "failed"}
          /\ paused \in BOOLEAN
          /\ pending \in [Names -> {"absent", "staged", "queued", "rejected", "opened"}]
=============================================================================
