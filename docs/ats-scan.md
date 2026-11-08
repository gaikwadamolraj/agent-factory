# requirement in detail

for ats journey i want this way - 
this is next target for ats: 


we should onboard candidate table and store all the information and his resume history there and create KG for the per candidate and once new profile updates need to update the KG 

so recruter will run the match for requirement JD against 100 profiles and score will calculate based on the JD vs profile - it will be faster as KG is there JD is there just match algo 

if recruter says show top 5 then show 5 profiles and if he want to talk to profile then chat window for profile should run and ans questions( as we doing the right now) 
if he ask overall 5 profile in run chat then 5 profiles overview for matched(get this data from runs happened and stored matching report).

expose the api that will accept the files so FE will send one by one as it should not cross the limit - show 10 files and progress list in the UI for doc upload sectoin 

in the BE accept the file and store firstly and make entry into the db with status properly like uploaded in progress etc 

then our next journey for pdf reader - like scan before and after extract, trigger the KG and next stage - create a candidate profile based on the candidate
- keda should be scale 1 profile at a time as we have limit of the process of infra and llm 

show the progress to user on the dasboard for each file with proper staage if any file failed to process allow retry button if not then show error for the file if success then show the green tick so they can see the details. same chat api should allow to chat with profile directly.

think some idea about jsonb column as possible so will reduce the rdbms schema.

does temporal help here?