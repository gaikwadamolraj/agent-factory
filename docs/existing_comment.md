



below are some existing observations:
3. allow user to find the profile for JD and get top listed 
    1. find appropriate profile then run the check again JD and score it 
  4. as of now if ask for any other question like write a code is doing so we should limit with guard rail that only resones related with application should response instead of someting else. handle this too
5. agent-common - its services\common\src\agent_common - src\agent_common
6. logging - corealtion id - from user rquets to till to affected down system - global tracking journey - name the bset 
   also if chat between agents then create anew chat id so that we can control iteratoin additional id 
   for the UI - add chat id in the browser so direct links works same for other use case possible if
7. BYOA - concept we have to make the mechanism either use there login or if ours then for long running jobs we have to allow refresh token - think here, they may be MCP(http-steam or sse if old) or a2a or may be http call also not sure
8. use all the jwt standards so we can compliant for all - like iss public url for jwks to vlaidate if they want
9. svc to svc token use token for created service only not for all - a -> b not b -> c only b -> a.
10. JWKS public url - cache the content so no load system once we rotate keys refresh tings
11. secrets not mount to file - # A `*_FILE` variable pointing at a readable file wins (k8s mounts a Secret as a file for larger values). - runtime only fetch 
12. set up this on our env atleast: OpenBaoSecretProvider
13. access policy - allowed for time slots - auto reset -temp system
14. notify by email, text msg whats slack team etc via agent - notification to UI chat 
15. when we say mcp tools as api wrapper expose then we have to apply the tool policyh on the api too 
16. BodySizeLimitMiddleware - same config from gatway so one location change will reflect both locatoin - middleware + gatway 
17. ats.auth - not only for ats so rename to proper name agent_factory is our main and ats is the one of the client. agent_llm_calls_total - one more name agent_llm_call_duration_seconds 
18. agent-document - we have to think of the pdf file contain the table chart image - strategy for the same, allow orchestrator to pass prompt what he want the data. if tabular data then csv file etc.
19. if such data comes in pdf file like tabular large data and user is going to ask the ques like average of the data how to do - dicsuss this with me - my idea we can store the jsonb in pg at this stage and then use nl2sql with data.
20. if file is csv of rdbms data then we have to create a table and store the data and then using nl2sql return data. get the proper dteails for relation between tables, colmn names details for good quality response. while ingestion make the data sense if value not provided means get some sample group type data then create own(i am doing like dbt lake house - genie ) - very big idea and need to create a sepreate agent for such scenaios - seperate agent - not in immedate plan but future
21. we need to think to create sepreate agent for this so we can enhance insted of in document agent - smeantic query sample for each table and common - also we can think use any trained model for the sql query (future). 
22. while creating the vector we should not hit every time to vector get from semantic cache - refresh data should support with versoin - 
23. db - we dont have who crated - created_by updated_by full audit for all the tables , to show public ids - uuid so no one cane predit the sequence or pattern - those ids we are providing and most secur client project user etc 
role: str = Field(default="recruiter", max_length=32) - we moving to common service to use proepr user types that we are going to create email -make mandotry so that we can keep dteails allow dummy .
still cant see agent-factory admin related tables here?
in ats tables
created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field() - repeated so crease base model and use in all - common audit table allow soft delete only metadata - 
jsonb column in all tables so we can keep key-value data without touching db for small things - for all the tables
services\agents\agent-skill\src\agent_skill\memory\models.py - jsonb so we can put summarisation of chat with bg job 
create index appropritately for each table 
access group users - think again all aspects and see what other people build user access model and do better way. follow IAM role policy kind of mechanism really liked the same.
create cache at repo level with index number so if user updated or deleted update cache - cache pattern per client not overall unless its agent_factory level - maintain this pattern

24. nvidia or gvisor sandbox agent are beter instead of doing the code and we can sacle with keda? 
 at this stage allow to run the python code only but get the py or runtime params files as data - like html render with json data or other things if the wnat to run.
25. agent_skill - this is ats related agent. think we have to create a common - all progress should come form temporal so track from there. 
26. handle the state at he orchetrator level - agent-skill is doing to checkin - pass the chat history to him in prompt for direct chat - 
27. chat summrisation we have to do instead of increased things again and again - k8s job or celery job based on the scale on keda -    
28. \services\agents\agent-skill\src\agent_skill\screening\grounding.py - hard coded job - very bad - we have to create a prompt proper and give llm if reuired execute the sandbox to run the code of python - revistit this file - as plan to make this as dynamic - lets discuss 
services\agents\agent-skill\src\agent_skill\screening\scoring.py - same for this.. create this file .py file for the skill and execute the runtime as json data pass from 