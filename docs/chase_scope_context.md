# Chase — scope & team (interview prep notes)

Spoken-form talking points for "describe your role / your team / the scale you
worked at / a cross-team project / what you owned." First-person, defensible,
generalized for confidentiality (no internal ticket IDs, repo names, or
hostnames). The structured version lives in the JPMorgan entry of
[data/experience_bank.yaml](../data/experience_bank.yaml).

**Wired:** this file is folded in as "SCOPE NOTES" grounding by the interview-prep
generator ([agents/application/interview_prep.py](../agents/application/interview_prep.py)),
which produces a per-interview brief into `data/interview_prep/` whenever an
application flips to "interview" (or run on demand via
`python -m scripts.generate_interview_prep`). It also remains your own
human-readable script to read before the interview.

---

1. **What I owned.** I was the founding/primary author of several FastAPI
   microservices on a regulated financial-data platform — a Snowflake
   query-execution service, an identity/token service, and an access-control
   allowlist service — owning roughly 60–70% of the commits as the top
   contributor on each.

2. **My team.** I worked inside a 20-plus-engineer platform org; the flagship
   service alone had 23 contributors. On the infrastructure side I co-owned the
   Terraform with a couple of infra leads, where I was consistently the #1–#3
   author.

3. **The scale I operated at.** Everything ran on AWS ECS Fargate across four
   environments (dev/test/preprod/prod) in four separate AWS accounts in
   us-east-1, fronted by ALBs with ACM TLS, with state in PAY_PER_REQUEST
   DynamoDB tables. I owned a 7-module Terraform layout (base, connect, data,
   ecs, roles, route53, terratest).

4. **A cross-team collaboration.** I led the NextGen ECS migration — rolling it
   out across all four environments using an ALB listener-priority swap for a
   zero-downtime cutover and rewiring the Jib/Spinnaker pipelines — coordinating
   through Bitbucket gitflow release trains shared with the infra team.

5. **What I did steady-state.** Beyond projects, I ran the recurring release
   trains (dated preprod/prod release branches), owned the CI/CD config
   (Jenkins/Spinnaker/Jib/Skaffold/Docker), maintained Terraform across all
   environments, and handled TLS/security upkeep like SSL-policy tightening and
   resilient cert-bundle resolution.
