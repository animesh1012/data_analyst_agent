import streamlit as st
import pandas as pd
from typing import Literal
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langchain_community.utilities import SQLDatabase
from sqlalchemy import create_engine
from langchain.chat_models import init_chat_model
from langchain_community.agent_toolkits import SQLDatabaseToolkit
import json
from typing import Dict, List, Optional
import re
from prompts import SQL_GENERATOR_PROMPT, SQL_CHECKER_PROMPT



##Load SQLite DB
@st.cache_resource()
def load_sqlite_db():
    engine = create_engine("sqlite:///covid_19_report.db")
    db = SQLDatabase(engine=engine)
    return db


##Load LLM from AWS Bedrock
@st.cache_resource()
def load_llm():

    llm = init_chat_model("us.anthropic.claude-sonnet-4-20250514-v1:0",
                    model_provider="bedrock",
                    aws_access_key_id="AWS_ACCESS_KEY",
                    aws_secret_access_key="AWS_SECRET_KEY",
                    region_name='AWS_REGION',temperature=0,
                    )
    return llm

##Get all SQL tools
@st.cache_resource()
def get_tools():
     db = load_sqlite_db()
     llm = load_llm()
     toolkit = SQLDatabaseToolkit(db=db, llm=llm)
     tools = toolkit.get_tools()
     return tools



## AGENT STATES


##Agentic Flow utils

# Listing All tables
def list_tables(state: MessagesState):
    ##Manually Adding Tool Name
    tool_call = {
        "name": "sql_db_list_tables",
        "args": {},
        "id": "abc123",
        "type": "tool_call",
    }
    tools = get_tools()
    tool_call_message = AIMessage(content="", tool_calls=[tool_call])

    list_tables_tool = next(tool for tool in tools if tool.name == "sql_db_list_tables")
    tool_message = list_tables_tool.invoke(tool_call)
    response = AIMessage(f"Available tables: {tool_message.content}")

    return {"messages": [tool_call_message, tool_message, response]}


# Generating Tool call as AI Message
def call_get_schema(state: MessagesState):
    llm = load_llm()
    tools = get_tools()
    get_schema_tool = next(tool for tool in tools if tool.name == "sql_db_schema")
    llm_with_tools = llm.bind_tools([get_schema_tool], tool_choice="any")
    response = llm_with_tools.invoke(state["messages"])

    return {"messages": [response]}


##Generate SQL Query
def generate_query(state: MessagesState):

    db = load_sqlite_db()
    llm = load_llm()
    tools = get_tools()

    generate_query_system_prompt = SQL_GENERATOR_PROMPT.format(
                                                        dialect=db.dialect,
                                                        top_k=10,
                                                    )
    system_message = {
        "role": "system",
        "content": generate_query_system_prompt,
    }
    
    run_query_tool = next(tool for tool in tools if tool.name == "sql_db_query")
    llm_with_tools = llm.bind_tools([run_query_tool])
    response = llm_with_tools.invoke([system_message] + state["messages"])

    return {"messages": [response]}


##Check SQL Query
def check_query(state: MessagesState):

    db = load_sqlite_db()
    llm = load_llm()
    tools = get_tools()

    check_query_system_prompt = SQL_CHECKER_PROMPT.format(dialect=db.dialect)
    system_message = {
        "role": "system",
        "content": check_query_system_prompt,
    }

    tool_call = state["messages"][-1].tool_calls[0]
    user_message = {"role": "user", "content": tool_call["args"]["query"]}

    run_query_tool = next(tool for tool in tools if tool.name == "sql_db_query")
    llm_with_tools = llm.bind_tools([run_query_tool], tool_choice="any")
    response = llm_with_tools.invoke([system_message, user_message])
    response.id = state["messages"][-1].id

    return {"messages": [response]}


def should_continue(state: MessagesState) -> Literal["check_query", END]:
    last_message = state["messages"][-1]
    if last_message.tool_calls and last_message.tool_calls[0]["name"] == "sql_db_query":
        return "check_query"
    else:
        return END



##Build Graph
@st.cache_resource()
def build_graph():
    tools = get_tools()
    ##Extracting SQL Schema Tool from list of tools
    get_schema_tool = next(tool for tool in tools if tool.name == "sql_db_schema")
    get_schema_node = ToolNode([get_schema_tool], name="get_schema")

    ##Extracting SQL Schema Tool from list of tools
    run_query_tool = next(tool for tool in tools if tool.name == "sql_db_query")
    run_query_node = ToolNode([run_query_tool], name="run_query")

    builder = StateGraph(MessagesState)
    builder.add_node(list_tables)
    builder.add_node(call_get_schema)
    builder.add_node(get_schema_node, "get_schema")
    builder.add_node(generate_query)
    builder.add_node(check_query)
    builder.add_node(run_query_node, "run_query")

    builder.add_edge(START, "list_tables")
    builder.add_edge("list_tables", "call_get_schema")
    builder.add_edge("call_get_schema", "get_schema")
    builder.add_edge("get_schema", "generate_query")
    builder.add_conditional_edges(
        "generate_query",
        should_continue,
    )
    builder.add_edge("check_query", "run_query")
    builder.add_edge("run_query", "generate_query")

    agent = builder.compile()
    return agent



def extract_all_queries(raw: str) -> List[str]:
    """
    Find every {"query": "..."} in `raw`, parse it as JSON
    to unescape any \\n, \\" etc., and return the list of SQL strings
    in the order they appear.
    """
    queries: List[str] = []
    QUERY_OBJ_RE = re.compile(r'\{\s*["\']query["\']\s*:\s*"(?:\\.|[^"\\])*"\s*\}')
    for m in QUERY_OBJ_RE.finditer(raw):
        obj_text = m.group(0)
        data = json.loads(obj_text)
        queries.append(data["query"])
    return queries

def extract_latest_query(raw: str) -> Optional[str]:
    """
    Return the last SQL query found, or None if none were present.
    """
    all_qs = extract_all_queries(raw)
    return all_qs[-1] if all_qs else None
 

@st.cache_resource()
def load_data_df():
     df = pd.read_csv("./worldometer_data.csv")
     return df





##STREAM REPONSE FUNCTION
def run_and_stream(thinking_placeholder, response_placeholder, input_message, chat_history, agent):
                        k = 0
                        sql_query = ""
                        thinking = ""
                        final_response = ""
                        start_marker = "<reasoning>"
                        end_marker = "</reasoning>"
                        start_final_marker = "<final_response>"
                        end_final_marker = "</final_response>"
                        is_thinking=True

                        for msg, metadata in agent.stream(
                            {
                                "messages": chat_history[-8:] + [
                                    {"role": "user", "content": input_message},
                                ],
                            },
                            stream_mode="messages",
                            config={"recursion_limit": 100},
                        ):
                            if not msg.content or metadata.get("langgraph_node") != "generate_query":
                                continue

                            token = msg.content[0].get("text", "")
                            thinking += token
        
                            if is_thinking and thinking:
                                    ##Take first text after start marker
                                    if start_marker in thinking:
                                        before, after = thinking.split(start_marker, 1)
                                        thinking = before.strip() + after.strip()
                                    if end_marker in thinking:
                                        before, after = thinking.split(end_marker, 1)
                                        thinking = before.strip() + after.strip()

                                    
                                    if start_final_marker in thinking:
                                         is_thinking = False
                                         before, after = thinking.split(start_final_marker, 1)
                                         thinking = before.strip()
                                         final_response += after.strip()
                                    thinking_placeholder.markdown(f"""
                                            <div style="
                                                background-color: black;    
                                                padding: 1rem;          
                                                border-radius: 8px;         
                                                margin: 0 -1rem 0 0.5rem;     
                                                width: calc(100% - 0.5rem);
                                                box-sizing: border-box;
                                            ">
                                            🧠 <strong>Thinking…</strong>
                                            <pre style="
                                                white-space: pre-wrap;
                                                word-break: break-word;
                                                margin: 0;
                                            ">
                                            {thinking}
                                            </pre>
                                            </div>
                                            """, unsafe_allow_html=True)

                            elif not is_thinking:
                                # streaming the “final” phase
                                final_response += token
                                if end_final_marker in final_response:
                                     before, _ = final_response.split(end_final_marker, 1)
                                     final_response = before.strip()
                                response_placeholder.markdown(final_response)

                            #Extracting SQL Query from msg
                            if msg.content and metadata["langgraph_node"] == "generate_query":
                                sql_token = msg.content[0].get('partial_json',"")
                                if sql_token:
                                    sql_query+=sql_token

                        return thinking, final_response, sql_query




##STREAMLIT UI STARTED

def main():

    st.set_page_config(page_title="SQL AGENT",
                        layout="centered",
                        page_icon="🤖")

    # --- App Title ---
    st.markdown("<h1 style='text-align: center;'>SQL AGENT</h1>", unsafe_allow_html=True)
    st.sidebar.markdown("<h4 style='text-align: left;'>Developed using LangGraph..</h4>", unsafe_allow_html=True)


        ##Messages and Chat history
    if "messages" not in st.session_state:
                st.session_state.messages=[]

    if "chat_history" not in st.session_state:
                st.session_state.chat_history=[]

    for message in st.session_state.messages:
                with st.chat_message(message['role']):
                    st.markdown(message['content'])
        
    #Graph Initialisation
    graph = build_graph()

    st.sidebar.subheader("Your Data Frame:")
    st.sidebar.dataframe(load_data_df())

    #Clear Memory Button
    if st.sidebar.button("Clear Agent Memory"):
        st.session_state.chat_history=[]
    

    user_input = st.chat_input('Ask your query here')
    if user_input:
                with st.chat_message('user'):
                    st.markdown(user_input)
                st.session_state.messages.append({'role':'user', 'content':user_input})

                with st.chat_message('assistant'):

                    # create two empty placeholders at the top of your Streamlit app
                    thinking_placeholder  = st.empty()
                    response_placeholder  = st.empty()
                    thinking_text, answer_text, sql_query = run_and_stream(thinking_placeholder, response_placeholder,
                                                                           user_input, st.session_state.chat_history,
                                                                           graph)


                st.session_state.messages.append({'role':'assistant', 'content':answer_text})
                st.sidebar.subheader("SQL QUERY")
                st.sidebar.code(extract_latest_query(sql_query), language='sql', wrap_lines=True)
                
                ##Adding into chat history
                st.session_state.chat_history.extend([
                {"role":"user",
                "content": user_input},
                {"role":"assistant",
                "content": answer_text}
            ])
                




if __name__ == "__main__":
     main()



            

    














