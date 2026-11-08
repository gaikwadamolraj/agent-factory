from __future__ import annotations
import os
from dotenv import load_dotenv
from mcp.client.stdio import StdioServerParameters, stdio_client
from strands import Agent
from strands.models.litellm import LiteLLMModel
from strands.tools.mcp import MCPClient

load_dotenv()


def create_dynamic_ontology_agent(mcp_client: MCPClient) -> Agent:
    """
    Creates a stateless Strands Agent bound dynamically to the Auto-Ontology MCP tools.
    """
    model = LiteLLMModel(
        model_id=f"openai/{os.getenv('REASONING_MODEL', 'nvidia/nemotron-3.5-lightning-30b-a3b')}",
        client_args={
            "api_key": os.getenv("LLM_API_KEY", "dummy-key"),
            "base_url": os.getenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        },
        params={"temperature": 0.1},
    )

    tools = mcp_client.list_tools_sync()

    return Agent(
        name="AutoOntologyAnalyst",
        model=model,
        tools=tools,
        system_prompt=(
            "You are a grounded enterprise data analyst powered by NVIDIA Auto-Ontology via MCP.\n"
            "Follow this strict protocol:\n"
            "1. If the target domain is not yet compiled, call `compile_ontology_from_db` first.\n"
            "2. Always call `search_ontology` to inspect certified business definitions, SQL formulas, "
            "and join relationships before writing any SQL.\n"
            "3. Never guess column meanings. Execute SQL strictly through `validate_and_execute_sql`.\n"
            "4. Include the certified term provenance in your final answer."
        ),
    )


def main() -> None:
    server_params = StdioServerParameters(command="python", args=["mcp_server.py", "--transport", "stdio"])
    mcp_client = MCPClient(lambda: stdio_client(server_params))

    with mcp_client:
        agent = create_dynamic_ontology_agent(mcp_client)

        # 1. Dynamically compile ontology & certify a strict business rule for Net Revenue
        setup_prompt = (
            "Compile the ontology for domain 'sales_demo' using the default database. "
            "Then register a certified metric 'net_revenue' on table 'orders' mapped to "
            "['gross_amt', 'discount_amt', 'order_status'] with sql_formula "
            "'SUM(gross_amt - discount_amt) WHERE order_status = \'COMPLETED\'' "
            "and definition 'Net completed revenue excluding cancelled orders and tax'."
        )
        print("=== STEP 1: DYNAMIC ONTOLOGY COMPILATION ===")
        print(agent(setup_prompt))

        # 2. Ask a grounded analytical question using the newly compiled semantic layer
        print("\n=== STEP 2: GROUNDED SEMANTIC QUERY ===")
        print(agent("What is our total net revenue by customer segment in domain 'sales_demo'?"))


if __name__ == "__main__":
    main()
