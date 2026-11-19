# RAWBerry document retrieval

RAWBerry answers questions using source documents from the conversation's corpus.

## Language

**Source document**: A file uploaded by a developer to a corpus as reference material for answers.

**Chunk**: A passage taken from a source document, retaining its document identity and available page location.

**Embedding**: A numeric representation of a chunk or question that supports matching passages by meaning.

**Vector store**: The collection of stored chunk embeddings used to find passages relevant to a question.

**Retrieval result**: A matching chunk, its source identity, and a similarity score. Similarity measures the match to a question, not answer truthfulness.

**System prompt**: A user's standing instruction for how RAWBerry should answer.

**Conversation history**: The stored transcript of a conversation.

**Conversation context**: The earlier completed turns RAWBerry includes when asking the model to answer a new question.

**Shared conversation**: A conversation the owner makes available for specific recipients to read.

**Retrieval match**: A percentage describing how closely the best retrieved passage matches the current question. It does not estimate answer correctness or whether the answer is supported.
_Avoid_: Model confidence, answer confidence

**Source citation**: A reference to a passage supplied as document context for an answer, including its document identity and available page and extracted text line range. It does not establish that the passage supports every claim in the answer.
