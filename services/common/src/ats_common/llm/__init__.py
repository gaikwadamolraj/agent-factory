"""One LLM layer for the platform (extra `llm`): settings, model aliases per purpose and client policy, the guarded
gateway client, structured completions and embeddings. See docs/llm_gateway.md.

    from ats_common.llm import Purpose, model_for, structured_completion, embed_query
"""
from ats_common.llm.calls import end_user, gateway_client, gateway_client_args, guarded, structured_completion
from ats_common.llm.embeddings import embed_documents, embed_query, embed_text, embed_texts, embedding_id
from ats_common.llm.models import Purpose, model_for, model_identity, profile_of
from ats_common.llm.settings import LLMSettings, get_llm_settings, reset_llm_settings

__all__ = ["LLMSettings", "Purpose", "embed_documents", "embed_query", "embed_text", "embed_texts", "embedding_id",
           "end_user", "gateway_client", "gateway_client_args", "get_llm_settings", "guarded", "model_for",
           "model_identity", "profile_of", "reset_llm_settings", "structured_completion"]
