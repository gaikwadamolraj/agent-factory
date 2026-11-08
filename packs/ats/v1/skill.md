---
name: ats-screening
description: Screen a candidate resume against a job description for any industry. Use for every request to evaluate, score, or screen a candidate.
---
# ATS screening workflow

The job description and resume for the current request are already loaded in your workspace; never ask for them and never paste them.

Call these tools exactly once each, in this order, waiting for each result:
1. analyze_job_description - turns the JD into atomic requirements.
2. analyze_candidate_profile - extracts verified skills and roles from the resume.
3. match_requirements - grades every requirement against the candidate's verified skills and computes the score.
4. write_verdict - writes the recruiter summary and interview questions.

If a tool returns an error, fix the cause (usually a missing earlier step) and call it again.
After write_verdict succeeds, reply with two plain sentences: the score with its band, and the main reason. Do not invent numbers; use the tool results.
