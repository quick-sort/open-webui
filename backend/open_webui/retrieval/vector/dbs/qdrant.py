"""
NOTE: This vector database integration is community-supported and maintained on a best-effort basis.
"""

from typing import Optional
import logging
import requests
from urllib.parse import urlparse

from qdrant_client import QdrantClient as Qclient
from qdrant_client.http.models import PointStruct
from qdrant_client.models import models

from open_webui.retrieval.vector.main import (
    VectorDBBase,
    VectorItem,
    SearchResult,
    GetResult,
)
from open_webui.config import (
    QDRANT_URI,
    QDRANT_API_KEY,
    QDRANT_ON_DISK,
    QDRANT_GRPC_PORT,
    QDRANT_GRPC_HTTPS,
    QDRANT_PREFER_GRPC,
    QDRANT_COLLECTION_PREFIX,
    QDRANT_TIMEOUT,
    QDRANT_HNSW_M,
    QDRANT_HNSW_EF_CONSTRUCT,
    QDRANT_HNSW_ON_DISK,
    QDRANT_QUANTIZATION,
    QDRANT_QUANTIZATION_SCALAR_TYPE,
    QDRANT_QUANTIZATION_SCALAR_QUANTILE,
    QDRANT_QUANTIZATION_ALWAYS_RAM,
    QDRANT_HYBRID_SEARCH,
    QDRANT_SPARSE_EMBEDDING_API_URL,
    QDRANT_SPARSE_EMBEDDING_API_KEY,
    QDRANT_SPARSE_EMBEDDING_MODEL,
)

NO_LIMIT = 999999999
DENSE_VECTOR_NAME = 'dense'
SPARSE_VECTOR_NAME = 'sparse'

log = logging.getLogger(__name__)


def _generate_sparse_vector(text: str) -> Optional[models.SparseVector]:
    """Call external sparse embedding API to generate a sparse vector."""
    if not QDRANT_SPARSE_EMBEDDING_API_URL or not QDRANT_SPARSE_EMBEDDING_MODEL:
        return None
    try:
        headers = {'Content-Type': 'application/json'}
        if QDRANT_SPARSE_EMBEDDING_API_KEY:
            headers['Authorization'] = f'Bearer {QDRANT_SPARSE_EMBEDDING_API_KEY}'
        r = requests.post(
            f'{QDRANT_SPARSE_EMBEDDING_API_URL}/embeddings',
            headers=headers,
            json={'input': text, 'model': QDRANT_SPARSE_EMBEDDING_MODEL},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        embedding = data['data'][0]['embedding']
        # Sparse embedding API returns list of {index, value} or a dict with indices/values
        if isinstance(embedding, dict):
            return models.SparseVector(
                indices=embedding['indices'],
                values=embedding['values'],
            )
        elif isinstance(embedding, list) and embedding and isinstance(embedding[0], dict):
            return models.SparseVector(
                indices=[e['index'] for e in embedding],
                values=[e['value'] for e in embedding],
            )
        else:
            log.warning(f'Unexpected sparse embedding format: {type(embedding)}')
            return None
    except Exception as e:
        log.exception(f'Error generating sparse vector: {e}')
        return None


def _generate_sparse_vectors_batch(texts: list[str]) -> list[Optional[models.SparseVector]]:
    """Batch generate sparse vectors for multiple texts."""
    if not QDRANT_SPARSE_EMBEDDING_API_URL or not QDRANT_SPARSE_EMBEDDING_MODEL:
        return [None] * len(texts)
    try:
        headers = {'Content-Type': 'application/json'}
        if QDRANT_SPARSE_EMBEDDING_API_KEY:
            headers['Authorization'] = f'Bearer {QDRANT_SPARSE_EMBEDDING_API_KEY}'
        r = requests.post(
            f'{QDRANT_SPARSE_EMBEDDING_API_URL}/embeddings',
            headers=headers,
            json={'input': texts, 'model': QDRANT_SPARSE_EMBEDDING_MODEL},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data['data']:
            embedding = item['embedding']
            if isinstance(embedding, dict):
                results.append(models.SparseVector(
                    indices=embedding['indices'],
                    values=embedding['values'],
                ))
            elif isinstance(embedding, list) and embedding and isinstance(embedding[0], dict):
                results.append(models.SparseVector(
                    indices=[e['index'] for e in embedding],
                    values=[e['value'] for e in embedding],
                ))
            else:
                results.append(None)
        return results
    except Exception as e:
        log.exception(f'Error generating sparse vectors batch: {e}')
        return [None] * len(texts)


class QdrantClient(VectorDBBase):
    def __init__(self):
        self.collection_prefix = QDRANT_COLLECTION_PREFIX
        self.QDRANT_URI = QDRANT_URI
        self.QDRANT_API_KEY = QDRANT_API_KEY
        self.QDRANT_ON_DISK = QDRANT_ON_DISK
        self.PREFER_GRPC = QDRANT_PREFER_GRPC
        self.GRPC_PORT = QDRANT_GRPC_PORT
        self.GRPC_HTTPS = QDRANT_GRPC_HTTPS
        self.QDRANT_TIMEOUT = QDRANT_TIMEOUT
        self.QDRANT_HNSW_M = QDRANT_HNSW_M
        self.QDRANT_HNSW_EF_CONSTRUCT = QDRANT_HNSW_EF_CONSTRUCT
        self.QDRANT_HNSW_ON_DISK = QDRANT_HNSW_ON_DISK
        self.QUANTIZATION = QDRANT_QUANTIZATION
        self.QUANTIZATION_SCALAR_TYPE = QDRANT_QUANTIZATION_SCALAR_TYPE
        self.QUANTIZATION_SCALAR_QUANTILE = QDRANT_QUANTIZATION_SCALAR_QUANTILE
        self.QUANTIZATION_ALWAYS_RAM = QDRANT_QUANTIZATION_ALWAYS_RAM
        self.HYBRID_SEARCH = QDRANT_HYBRID_SEARCH

        if not self.QDRANT_URI:
            self.client = None
            return

        # Unified handling for either scheme
        parsed = urlparse(self.QDRANT_URI)
        host = parsed.hostname or self.QDRANT_URI
        http_port = parsed.port or 6333  # default REST port

        if self.PREFER_GRPC:
            self.client = Qclient(
                host=host,
                port=http_port,
                grpc_port=self.GRPC_PORT,
                prefer_grpc=self.PREFER_GRPC,
                api_key=self.QDRANT_API_KEY,
                timeout=self.QDRANT_TIMEOUT,
                https=self.GRPC_HTTPS,
            )
        else:
            self.client = Qclient(
                url=self.QDRANT_URI,
                api_key=self.QDRANT_API_KEY,
                timeout=QDRANT_TIMEOUT,
            )

    def _result_to_get_result(self, points) -> GetResult:
        ids = []
        documents = []
        metadatas = []

        for point in points:
            payload = point.payload
            ids.append(point.id)
            documents.append(payload['text'])
            metadatas.append(payload['metadata'])

        return GetResult(
            **{
                'ids': [ids],
                'documents': [documents],
                'metadatas': [metadatas],
            }
        )

    def _get_quantization_config(self) -> Optional[models.QuantizationConfig]:
        match self.QUANTIZATION:
            case 'scalar':
                return models.ScalarQuantization(
                    scalar=models.ScalarQuantizationConfig(
                        type=models.ScalarType.INT8,
                        quantile=self.QUANTIZATION_SCALAR_QUANTILE,
                        always_ram=self.QUANTIZATION_ALWAYS_RAM,
                    ),
                )
            case 'binary':
                return models.BinaryQuantization(
                    binary=models.BinaryQuantizationConfig(
                        always_ram=self.QUANTIZATION_ALWAYS_RAM,
                    ),
                )
            case 'product':
                return models.ProductQuantization(
                    product=models.ProductQuantizationConfig(
                        num_subquantizers=256,
                        num_centroids=16,
                    ),
                )
            case _:
                return None

    def _create_collection(self, collection_name: str, dimension: int):
        collection_name_with_prefix = f'{self.collection_prefix}_{collection_name}'

        if self.HYBRID_SEARCH:
            vectors_config = {
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=dimension,
                    distance=models.Distance.COSINE,
                    on_disk=self.QDRANT_ON_DISK,
                ),
            }
            sparse_vectors_config = {
                SPARSE_VECTOR_NAME: models.SparseVectorParams(),
            }
        else:
            vectors_config = models.VectorParams(
                size=dimension,
                distance=models.Distance.COSINE,
                on_disk=self.QDRANT_ON_DISK,
            )
            sparse_vectors_config = None

        self.client.create_collection(
            collection_name=collection_name_with_prefix,
            vectors_config=vectors_config,
            sparse_vectors_config=sparse_vectors_config,
            hnsw_config=models.HnswConfigDiff(
                m=self.QDRANT_HNSW_M,
                ef_construct=self.QDRANT_HNSW_EF_CONSTRUCT,
                on_disk=self.QDRANT_HNSW_ON_DISK,
            ),
            quantization_config=self._get_quantization_config(),
        )

        # Create payload indexes for efficient filtering
        self.client.create_payload_index(
            collection_name=collection_name_with_prefix,
            field_name='metadata.hash',
            field_schema=models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD,
                is_tenant=False,
                on_disk=self.QDRANT_ON_DISK,
            ),
        )
        self.client.create_payload_index(
            collection_name=collection_name_with_prefix,
            field_name='metadata.file_id',
            field_schema=models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD,
                is_tenant=False,
                on_disk=self.QDRANT_ON_DISK,
            ),
        )
        log.info(f'collection {collection_name_with_prefix} successfully created!')

    def _create_collection_if_not_exists(self, collection_name, dimension):
        if not self.has_collection(collection_name=collection_name):
            self._create_collection(collection_name=collection_name, dimension=dimension)

    def _create_points(self, items: list[VectorItem]):
        if self.HYBRID_SEARCH:
            texts = [item['text'] for item in items]
            sparse_vectors = _generate_sparse_vectors_batch(texts)
            points = []
            for item, sv in zip(items, sparse_vectors):
                vector = {DENSE_VECTOR_NAME: item['vector']}
                if sv is not None:
                    vector[SPARSE_VECTOR_NAME] = sv
                points.append(PointStruct(
                    id=item['id'],
                    vector=vector,
                    payload={'text': item['text'], 'metadata': item['metadata']},
                ))
            return points
        else:
            return [
                PointStruct(
                    id=item['id'],
                    vector=item['vector'],
                    payload={'text': item['text'], 'metadata': item['metadata']},
                )
                for item in items
            ]

    def has_collection(self, collection_name: str) -> bool:
        return self.client.collection_exists(f'{self.collection_prefix}_{collection_name}')

    def delete_collection(self, collection_name: str):
        return self.client.delete_collection(collection_name=f'{self.collection_prefix}_{collection_name}')

    def search(
        self,
        collection_name: str,
        vectors: list[list[float | int]],
        filter: Optional[dict] = None,
        limit: int = 10,
        **kwargs,
    ) -> Optional[SearchResult]:
        # Search for the nearest neighbor items based on the vectors and return 'limit' number of results.
        if limit is None:
            limit = NO_LIMIT  # otherwise qdrant would set limit to 10!

        collection = f'{self.collection_prefix}_{collection_name}'
        query_text = kwargs.get('query_text')

        # Hybrid search: prefetch dense + sparse, fuse with RRF
        if self.HYBRID_SEARCH and query_text:
            sparse_vector = _generate_sparse_vector(query_text)
            if sparse_vector is not None:
                prefetch_limit = limit * 2
                query_response = self.client.query_points(
                    collection_name=collection,
                    prefetch=[
                        models.Prefetch(
                            query=vectors[0],
                            using=DENSE_VECTOR_NAME,
                            limit=prefetch_limit,
                        ),
                        models.Prefetch(
                            query=sparse_vector,
                            using=SPARSE_VECTOR_NAME,
                            limit=prefetch_limit,
                        ),
                    ],
                    query=models.FusionQuery(fusion=models.Fusion.RRF),
                    limit=limit,
                )
                get_result = self._result_to_get_result(query_response.points)
                return SearchResult(
                    ids=get_result.ids,
                    documents=get_result.documents,
                    metadatas=get_result.metadatas,
                    distances=[[(point.score + 1.0) / 2.0 for point in query_response.points]],
                )

        # Fallback: dense-only search
        query_args = {
            'collection_name': collection,
            'query': vectors[0],
            'limit': limit,
        }
        if self.HYBRID_SEARCH:
            query_args['using'] = DENSE_VECTOR_NAME

        query_response = self.client.query_points(**query_args)
        get_result = self._result_to_get_result(query_response.points)
        return SearchResult(
            ids=get_result.ids,
            documents=get_result.documents,
            metadatas=get_result.metadatas,
            # qdrant distance is [-1, 1], normalize to [0, 1]
            distances=[[(point.score + 1.0) / 2.0 for point in query_response.points]],
        )

    def query(self, collection_name: str, filter: dict, limit: Optional[int] = None):
        # Construct the filter string for querying
        if not self.has_collection(collection_name):
            return None
        try:
            if limit is None:
                limit = NO_LIMIT  # otherwise qdrant would set limit to 10!

            field_conditions = []
            for key, value in filter.items():
                field_conditions.append(
                    models.FieldCondition(key=f'metadata.{key}', match=models.MatchValue(value=value))
                )

            points = self.client.scroll(
                collection_name=f'{self.collection_prefix}_{collection_name}',
                scroll_filter=models.Filter(should=field_conditions),
                limit=limit,
            )
            return self._result_to_get_result(points[0])
        except Exception as e:
            log.exception(f"Error querying a collection '{collection_name}': {e}")
            return None

    def get(self, collection_name: str) -> Optional[GetResult]:
        # Get all the items in the collection.
        points = self.client.scroll(
            collection_name=f'{self.collection_prefix}_{collection_name}',
            limit=NO_LIMIT,  # otherwise qdrant would set limit to 10!
        )
        return self._result_to_get_result(points[0])

    def insert(self, collection_name: str, items: list[VectorItem]):
        # Insert the items into the collection, if the collection does not exist, it will be created.
        self._create_collection_if_not_exists(collection_name, len(items[0]['vector']))
        points = self._create_points(items)
        self.client.upload_points(f'{self.collection_prefix}_{collection_name}', points)

    def upsert(self, collection_name: str, items: list[VectorItem]):
        # Update the items in the collection, if the items are not present, insert them. If the collection does not exist, it will be created.
        self._create_collection_if_not_exists(collection_name, len(items[0]['vector']))
        points = self._create_points(items)
        return self.client.upsert(f'{self.collection_prefix}_{collection_name}', points)

    def delete(
        self,
        collection_name: str,
        ids: Optional[list[str]] = None,
        filter: Optional[dict] = None,
    ):
        # Delete the items from the collection based on the ids.
        field_conditions = []

        if ids:
            for id_value in ids:
                (
                    field_conditions.append(
                        models.FieldCondition(
                            key='metadata.id',
                            match=models.MatchValue(value=id_value),
                        ),
                    ),
                )
        elif filter:
            for key, value in filter.items():
                (
                    field_conditions.append(
                        models.FieldCondition(
                            key=f'metadata.{key}',
                            match=models.MatchValue(value=value),
                        ),
                    ),
                )

        return self.client.delete(
            collection_name=f'{self.collection_prefix}_{collection_name}',
            points_selector=models.FilterSelector(filter=models.Filter(must=field_conditions)),
        )

    def reset(self):
        # Resets the database. This will delete all collections and item entries.
        collection_names = self.client.get_collections().collections
        for collection_name in collection_names:
            if collection_name.name.startswith(self.collection_prefix):
                self.client.delete_collection(collection_name=collection_name.name)
