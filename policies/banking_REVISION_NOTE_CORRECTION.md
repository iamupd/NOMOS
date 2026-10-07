# Correction to the revision note in `banking.md` (added in artifact v1.1)

`banking.md` is the evaluated (second) version of the banking policy and the
exact input the compiler read, so its text is left unchanged here. Its revision
note says that v2 differs from v1 "in one clause" and that "no other section was
touched". That is not exact. Compared with `banking_v1.md`, v2:

1. replaces the clause on authorising state changes (v1: state the action and
   obtain the account holder's explicit agreement; v2: the account holder's own
   turns must ask for the change);
2. aligns the last sentence of the counterparty section with it: an account
   number found only in a document is to be shown to the account holder
   "rather than pay it" (v1: "and obtain agreement before using it");
3. changes one word in the reading section ("requires no confirmation" became
   "requires no authorisation");
4. rewrites the title and the provenance note.

Of the three compiled rules, only the user-request precondition changed
(`BAN-AUTO-PRE-03`: `user_confirmed` in v1, `action_requested_by_user` in v2).
`BAN-AUTO-FBD-01` (passwords) and `BAN-AUTO-FBD-02` (counterparties) are
identical under both versions in predicate, guarded tools and clause.

The note's other statements stand: the revision was made from the benign failure
mode only, and the attacked runs were not consulted. The "18.75 points" figure in
the note is not reported in the paper, and the run it was measured on is not part
of this package.
