from typing import List, Dict, Any

import marqo.core.search.search_filter as search_filter
from marqo.core.exceptions import InvalidDataTypeError, InvalidFieldNameError, VespaDocumentParsingError
from marqo.core.models import MarqoQuery
from marqo.core.models.marqo_index import *
from marqo.core.models.marqo_query import MarqoTensorQuery, MarqoLexicalQuery, MarqoHybridQuery, ScoreModifierType
from marqo.core.structured_vespa_index import common
from marqo.core.vespa_index import VespaIndex
from marqo.exceptions import InternalError


class StructuredVespaIndex(VespaIndex):
    """
    An implementation of VespaIndex for structured indexes.
    """

    _MARQO_TO_PYTHON_TYPE_MAP = {
        FieldType.Text: str,
        FieldType.Bool: bool,
        FieldType.Int: int,
        FieldType.Float: [float, int],
        FieldType.ArrayText: list,
        FieldType.ArrayInt: list,
        FieldType.ArrayFloat: list,
        FieldType.ImagePointer: str,
        FieldType.MultimodalCombination: dict
    }

    _VESPA_DOC_ID = 'id'
    _VESPA_DOC_FIELDS = 'fields'
    _VESPA_DOC_RELEVANCE = 'relevance'
    _VESPA_DOC_MATCH_FEATURES = 'matchfeatures'
    _VESPA_DOC_FIELDS_TO_IGNORE = {'sddocname'}

    def __init__(self, marqo_index: StructuredMarqoIndex):
        self._marqo_index = marqo_index

    def to_vespa_document(self, marqo_document: Dict[str, Any]) -> Dict[str, Any]:
        vespa_id: Optional[int] = None
        vespa_fields: Dict[str, Any] = dict()
        score_modifiers: Dict[str, float] = {}

        # ID
        if constants.MARQO_DOC_ID in marqo_document:
            vespa_id = marqo_document[constants.MARQO_DOC_ID]
            vespa_fields[common.FIELD_ID] = vespa_id

        # Fields
        for marqo_field in marqo_document:
            if marqo_field == constants.MARQO_DOC_TENSORS or marqo_field == constants.MARQO_DOC_ID:
                continue  # process tensor fields later

            marqo_value = marqo_document[marqo_field]
            self._verify_marqo_field_name(marqo_field)
            self._verify_marqo_field_type(marqo_field, marqo_value)

            index_field = self._marqo_index.field_map[marqo_field]

            if index_field.type == FieldType.Bool:
                # Booleans are stored as bytes in Vespa
                marqo_value = int(marqo_value)

            if index_field.lexical_field_name:
                vespa_fields[index_field.lexical_field_name] = marqo_value
            if index_field.filter_field_name:
                vespa_fields[index_field.filter_field_name] = marqo_value
            if not index_field.lexical_field_name and not index_field.filter_field_name:
                vespa_fields[index_field.name] = marqo_value

            if FieldFeature.ScoreModifier in index_field.features:
                score_modifiers[index_field.name] = marqo_value

        # Tensors
        vector_count = 0
        if constants.MARQO_DOC_TENSORS in marqo_document:
            for marqo_tensor_field in marqo_document[constants.MARQO_DOC_TENSORS]:
                marqo_tensor_value = marqo_document[constants.MARQO_DOC_TENSORS][marqo_tensor_field]

                self._verify_marqo_tensor_field_name(marqo_tensor_field)
                self._verify_marqo_tensor_field(marqo_tensor_field, marqo_tensor_value)

                # If chunking an image, chunks will be a list of tuples, hence the str(c)
                chunks = [str(c) for c in marqo_tensor_value[constants.MARQO_DOC_CHUNKS]]
                embeddings = marqo_tensor_value[constants.MARQO_DOC_EMBEDDINGS]
                vector_count += len(embeddings)

                index_tensor_field = self._marqo_index.tensor_field_map[marqo_tensor_field]

                vespa_fields[index_tensor_field.chunk_field_name] = chunks
                vespa_fields[index_tensor_field.embeddings_field_name] = \
                    {f'{i}': embeddings[i] for i in range(len(embeddings))}

        vespa_fields[common.FIELD_VECTOR_COUNT] = vector_count

        if len(score_modifiers) > 0:
            vespa_fields[common.FIELD_SCORE_MODIFIERS] = score_modifiers

        vespa_doc = {
            self._VESPA_DOC_FIELDS: vespa_fields
        }

        if vespa_id is not None:
            vespa_doc[self._VESPA_DOC_ID] = vespa_id

        return vespa_doc

    def to_marqo_document(
            self, vespa_document: Dict[str, Any], return_highlights: bool = False
    ) -> Dict[str, Any]:

        if self._VESPA_DOC_FIELDS not in vespa_document:
            raise VespaDocumentParsingError(f'Vespa document is missing {self._VESPA_DOC_FIELDS} field')

        fields = vespa_document[self._VESPA_DOC_FIELDS]
        marqo_document = dict()
        for field, value in fields.items():
            if field in self._marqo_index.all_field_map:
                marqo_field = self._marqo_index.all_field_map[field]

                if marqo_field.type == FieldType.Bool:
                    # Booleans are stored as bytes in Vespa
                    if value not in {0, 1}:
                        raise VespaDocumentParsingError(
                            f"Vespa document has invalid value '{value}' for boolean field '{marqo_field.name}'. "
                            f'Expected 0 or 1'
                        )
                    value = bool(value)

                marqo_name = marqo_field.name
                if marqo_name in marqo_document:
                    # If getting all fields from Vespa, there may be a lexical and a filter field for one Marqo field
                    # They must have the same value
                    if marqo_document[marqo_name] != value:
                        raise VespaDocumentParsingError(
                            f'Vespa document has different values for Marqo field {marqo_name}: '
                            f'{marqo_document[marqo_name]} and {value}'
                        )
                else:

                    marqo_document[marqo_name] = value
            elif field in self._marqo_index.tensor_subfield_map:
                tensor_field = self._marqo_index.tensor_subfield_map[field]

                if constants.MARQO_DOC_TENSORS not in marqo_document:
                    marqo_document[constants.MARQO_DOC_TENSORS] = dict()
                if tensor_field.name not in marqo_document[constants.MARQO_DOC_TENSORS]:
                    marqo_document[constants.MARQO_DOC_TENSORS][tensor_field.name] = dict()

                if field == tensor_field.chunk_field_name:
                    marqo_document[constants.MARQO_DOC_TENSORS][tensor_field.name][constants.MARQO_DOC_CHUNKS] = value
                elif field == tensor_field.embeddings_field_name:
                    try:
                        marqo_document[constants.MARQO_DOC_TENSORS][tensor_field.name][
                            constants.MARQO_DOC_EMBEDDINGS] = list(value['blocks'].values())
                    except (KeyError, AttributeError, TypeError) as e:
                        raise VespaDocumentParsingError(
                            f'Cannot parse embeddings field {field} with value {value}'
                        ) from e

                else:
                    raise VespaDocumentParsingError(f'Unexpected tensor subfield {field}')
            elif field == common.FIELD_ID:
                marqo_document[constants.MARQO_DOC_ID] = value
            elif field == self._VESPA_DOC_MATCH_FEATURES:
                continue
            elif field in self._VESPA_DOC_FIELDS_TO_IGNORE | {common.FIELD_SCORE_MODIFIERS, common.FIELD_VECTOR_COUNT,
                                                              self._VESPA_DOC_MATCH_FEATURES}:
                continue
            else:
                raise VespaDocumentParsingError(
                    f'Unknown field {field} for index {self._marqo_index.name} in Vespa document'
                )

        # Highlights
        if return_highlights and self._VESPA_DOC_MATCH_FEATURES in fields:
            marqo_document[constants.MARQO_DOC_HIGHLIGHTS] = self._extract_highlights(
                fields
            )

        return marqo_document

    def to_vespa_query(self, marqo_query: MarqoQuery) -> Dict[str, Any]:
        # TODO - There is some inefficiency here, as we are retrieving chunks even if highlights are false,
        # and also for lexical search. This applies to both with and without attributes_to_retrieve

        # Verify attributes to retrieve, if defined
        if marqo_query.attributes_to_retrieve is not None:
            chunk_field_names = []
            for att in marqo_query.attributes_to_retrieve:
                if att not in self._marqo_index.field_map:
                    raise InvalidFieldNameError(
                        f'Index {self._marqo_index.name} has no field {att}. '
                        f'Available fields are: {", ".join(self._marqo_index.field_map.keys())}'
                    )
                if att in self._marqo_index.tensor_field_map:
                    chunk_field_names.append(self._marqo_index.tensor_field_map[att].chunk_field_name)

            marqo_query.attributes_to_retrieve.append(common.FIELD_ID)
            marqo_query.attributes_to_retrieve.extend(chunk_field_names)

        # Verify score modifiers, if defined
        if marqo_query.score_modifiers is not None:
            for modifier in marqo_query.score_modifiers:
                if modifier.field not in self._marqo_index.score_modifier_fields_names:
                    raise InvalidFieldNameError(
                        f'Index {self._marqo_index.name} has no score modifier field {modifier.field}. '
                        f'Available score modifier fields are: {", ".join(self._marqo_index.score_modifier_fields_names)}'
                    )

        if isinstance(marqo_query, MarqoTensorQuery):
            return self._to_vespa_tensor_query(marqo_query)
        elif isinstance(marqo_query, MarqoLexicalQuery):
            return self._to_vespa_lexical_query(marqo_query)
        elif isinstance(marqo_query, MarqoHybridQuery):
            return self._to_vespa_hybrid_query(marqo_query)
        else:
            raise InternalError(f'Unknown query type {type(marqo_query)}')

    def get_vector_count_query(self):
        return {
            'yql': f'select {common.FIELD_VECTOR_COUNT} from {self._marqo_index.schema_name} '
                   f'where true limit 0 | all(group(1) each(output(sum({common.FIELD_VECTOR_COUNT}))))',
            'model_restrict': self._marqo_index.schema_name,
            'timeout': '5s'
        }

    def _to_vespa_tensor_query(self, marqo_query: MarqoTensorQuery) -> Dict[str, Any]:
        if marqo_query.searchable_attributes is not None:
            for att in marqo_query.searchable_attributes:
                if att not in self._marqo_index.tensor_field_map:
                    raise InvalidFieldNameError(
                        f'Index {self._marqo_index.name} has no tensor field {att}. '
                        f'Available tensor fields are: {", ".join(self._marqo_index.tensor_field_map.keys())}'
                    )

            fields_to_search = marqo_query.searchable_attributes
        else:
            fields_to_search = self._marqo_index.tensor_field_map.keys()

        tensor_term = self._get_tensor_search_term(marqo_query) if fields_to_search else "False"
        filter_term = self._get_filter_term(marqo_query)
        if filter_term:
            filter_term = f' AND {filter_term}'
        else:
            filter_term = ''
        select_attributes = self._get_select_attributes(marqo_query)
        summary = common.SUMMARY_ALL_VECTOR if marqo_query.expose_facets else common.SUMMARY_ALL_NON_VECTOR
        score_modifiers = self._get_score_modifiers(marqo_query)
        ranking = common.RANK_PROFILE_EMBEDDING_SIMILARITY_MODIFIERS if score_modifiers \
            else common.RANK_PROFILE_EMBEDDING_SIMILARITY

        query_inputs = {
            common.QUERY_INPUT_EMBEDDING: marqo_query.vector_query
        }
        query_inputs.update({
            f: 1 for f in fields_to_search
        })
        if score_modifiers:
            query_inputs.update(score_modifiers)

        query = {
            'yql': f'select {select_attributes} from {self._marqo_index.schema_name} where {tensor_term}{filter_term}',
            'model_restrict': self._marqo_index.schema_name,
            'hits': marqo_query.limit,
            'offset': marqo_query.offset,
            'query_features': query_inputs,
            'presentation.summary': summary,
            'ranking': ranking
        }
        query = {k: v for k, v in query.items() if v is not None}

        if not marqo_query.approximate:
            query['ranking.softtimeout.enable'] = False
            query['timeout'] = '300s'

        return query

    def _to_vespa_lexical_query(self, marqo_query: MarqoLexicalQuery) -> Dict[str, Any]:
        if marqo_query.searchable_attributes is not None:
            for att in marqo_query.searchable_attributes:
                if att not in self._marqo_index.lexically_searchable_fields_names:
                    raise InvalidFieldNameError(
                        f'Index {self._marqo_index.name} has no lexically searchable field {att}. '
                        f'Available lexically searchable fields are: '
                        f'{", ".join(self._marqo_index.lexically_searchable_fields_names)}'
                    )
            fields_to_search = marqo_query.searchable_attributes
        else:
            fields_to_search = self._marqo_index.lexical_field_map.keys()

        lexical_term = self._get_lexical_search_term(marqo_query) if fields_to_search else "False"
        filter_term = self._get_filter_term(marqo_query)
        if filter_term:
            filter_term = f' AND {filter_term}'
        else:
            filter_term = ''

        select_attributes = self._get_select_attributes(marqo_query)
        summary = common.SUMMARY_ALL_VECTOR if marqo_query.expose_facets else common.SUMMARY_ALL_NON_VECTOR
        score_modifiers = self._get_score_modifiers(marqo_query)
        ranking = common.RANK_PROFILE_BM25_MODIFIERS if score_modifiers \
            else common.RANK_PROFILE_BM25

        query_inputs = {}
        query_inputs.update({
            f: 1 for f in fields_to_search
        })
        if score_modifiers:
            query_inputs.update(score_modifiers)

        query = {
            'yql': f'select {select_attributes} from {self._marqo_index.schema_name} where {lexical_term}{filter_term}',
            'model_restrict': self._marqo_index.schema_name,
            'hits': marqo_query.limit,
            'offset': marqo_query.offset,
            'query_features': query_inputs,
            'presentation.summary': summary,
            'ranking': ranking
        }
        query = {k: v for k, v in query.items() if v is not None}

        return query

    def _to_vespa_hybrid_query(self, marqo_query: MarqoHybridQuery) -> Dict[str, Any]:
        raise NotImplementedError()

    def _get_tensor_search_term(self, marqo_query: MarqoTensorQuery) -> str:
        if marqo_query.searchable_attributes is not None:
            fields_to_search = [f for f in marqo_query.searchable_attributes if f in self._marqo_index.tensor_field_map]
        else:
            fields_to_search = self._marqo_index.tensor_field_map.keys()

        if marqo_query.ef_search is not None:
            target_hits = min(marqo_query.limit + marqo_query.offset, marqo_query.ef_search)
            additional_hits = max(marqo_query.ef_search - (marqo_query.limit + marqo_query.offset), 0)
        else:
            target_hits = marqo_query.limit + marqo_query.offset
            additional_hits = 0

        terms = []
        for field in fields_to_search:
            tensor_field = self._marqo_index.tensor_field_map[field]
            embedding_field_name = tensor_field.embeddings_field_name
            terms.append(
                f'('
                f'{{'
                f'targetHits:{target_hits}, '
                f'approximate:{str(marqo_query.approximate)}, '
                f'hnsw.exploreAdditionalHits:{additional_hits}'
                f'}}'
                f'nearestNeighbor({embedding_field_name}, {common.QUERY_INPUT_EMBEDDING})'
                f')'
            )

        if terms:
            return f'({" OR ".join(terms)})'
        else:
            return ''

    def _get_filter_term(self, marqo_query: MarqoQuery) -> Optional[str]:
        def escape(s: str) -> str:
            return s.replace('\\', '\\\\').replace('"', '\\"')

        def tree_to_filter_string(node: search_filter.Node) -> str:
            if isinstance(node, search_filter.Operator):
                if isinstance(node, search_filter.And):
                    operator = 'AND'
                elif isinstance(node, search_filter.Or):
                    operator = 'OR'
                else:
                    raise InternalError(f'Unknown operator type {type(node)}')

                return f'({tree_to_filter_string(node.left)} {operator} {tree_to_filter_string(node.right)})'
            elif isinstance(node, search_filter.Modifier):
                if isinstance(node, search_filter.Not):
                    return f'!({tree_to_filter_string(node.modified)})'
                else:
                    raise InternalError(f'Unknown modifier type {type(node)}')
            elif isinstance(node, search_filter.Term):
                if node.field not in self._marqo_index.filterable_fields_names:
                    raise InvalidFieldNameError(
                        f'Index {self._marqo_index.name} has no filterable field {node.field}. '
                        f'Available filterable fields are: {", ".join(self._marqo_index.filterable_fields_names)}'
                    )

                marqo_field = self._marqo_index.all_field_map[node.field]

                if isinstance(node, search_filter.EqualityTerm):
                    node_value = node.value
                    if marqo_field.type == FieldType.Bool:
                        if node_value.lower() == 'true':
                            node_value = '1'
                        elif node_value.lower() == 'false':
                            node_value = '0'

                    return f'{marqo_field.filter_field_name} contains "{escape(node_value)}"'
                elif isinstance(node, search_filter.RangeTerm):
                    lower = f'{marqo_field.filter_field_name} >= {node.lower}' if node.lower is not None else None
                    upper = f'{marqo_field.filter_field_name} <= {node.upper}' if node.upper is not None else None
                    if lower and upper:
                        return f'({lower} AND {upper})'
                    elif lower:
                        return lower
                    elif upper:
                        return upper
                    else:
                        raise InternalError('RangeTerm has no lower or upper bound')

            raise InternalError(f'Unknown node type {type(node)}')

        if marqo_query.filter is not None:
            return tree_to_filter_string(marqo_query.filter.root)

    def _get_select_attributes(self, marqo_query: MarqoQuery) -> str:
        if marqo_query.attributes_to_retrieve is not None:
            return ', '.join(marqo_query.attributes_to_retrieve)
        else:
            return '*'

    def _get_score_modifiers(self, marqo_query: MarqoQuery) -> \
            Optional[Dict[str, Dict[str, float]]]:
        if marqo_query.score_modifiers:
            mult_tensor = {}
            add_tensor = {}
            for modifier in marqo_query.score_modifiers:
                if modifier.type == ScoreModifierType.Multiply:
                    mult_tensor[modifier.field] = modifier.weight
                elif modifier.type == ScoreModifierType.Add:
                    add_tensor[modifier.field] = modifier.weight
                else:
                    raise InternalError(f'Unknown score modifier type {modifier.type}')

            # Note one of these could be empty, but not both
            return {
                common.QUERY_INPUT_SCORE_MODIFIERS_MULT_WEIGHTS: mult_tensor,
                common.QUERY_INPUT_SCORE_MODIFIERS_ADD_WEIGHTS: add_tensor
            }

        return None

    def _get_lexical_search_term(self, marqo_query: MarqoLexicalQuery) -> str:
        if marqo_query.or_phrases:
            or_terms = 'weakAnd(%s)' % ', '.join([
                self._get_lexical_contains_term(phrase, marqo_query) for phrase in marqo_query.or_phrases
            ])
        else:
            or_terms = ''
        if marqo_query.and_phrases:
            and_terms = ' AND '.join([
                self._get_lexical_contains_term(phrase, marqo_query) for phrase in marqo_query.and_phrases
            ])
            if or_terms:
                and_terms = f' AND ({and_terms})'
        else:
            and_terms = ''

        return f'{or_terms}{and_terms}'

    def _get_lexical_contains_term(self, phrase, query: MarqoQuery) -> str:
        if query.searchable_attributes is not None:
            return ' OR '.join([
                f'{self._marqo_index.field_map[field].lexical_field_name} contains "{phrase}"'
                for field in query.searchable_attributes
            ])
        else:
            return f'default contains "{phrase}"'

    def _verify_marqo_field_name(self, field_name: str):
        field_map = self._marqo_index.field_map
        if field_name not in field_map:
            raise InvalidFieldNameError(f'Invalid field name {field_name} for index {self._marqo_index.name}. '
                                        f'Valid field names are {", ".join(field_map.keys())}')

    def _verify_marqo_tensor_field_name(self, field_name: str):
        tensor_field_map = self._marqo_index.tensor_field_map
        if field_name not in tensor_field_map:
            raise InvalidFieldNameError(f'Invalid tensor field name {field_name} for index {self._marqo_index.name}. '
                                        f'Valid tensor field names are {", ".join(tensor_field_map.keys())}')

    def _verify_marqo_tensor_field(self, field_name: str, field_value: Dict[str, Any]):
        if not set(field_value.keys()) == {constants.MARQO_DOC_CHUNKS, constants.MARQO_DOC_EMBEDDINGS}:
            raise InternalError(f'Invalid tensor field {field_name}. '
                                f'Expected keys {constants.MARQO_DOC_CHUNKS}, {constants.MARQO_DOC_EMBEDDINGS} '
                                f'but found {", ".join(field_value.keys())}')

    def _verify_marqo_field_type(self, field_name: str, value: Any):
        marqo_type = self._marqo_index.field_map[field_name].type
        python_type = self._get_python_type(marqo_type)
        if (
                isinstance(python_type, list) and not any(isinstance(value, t) for t in python_type) or
                not isinstance(python_type, list) and not isinstance(value, python_type)
        ):
            raise InvalidDataTypeError(f'Invalid value {value} for field {field_name} with Marqo type '
                                       f'{marqo_type.value}. Expected a value of type {python_type}, but found '
                                       f'{type(value)}')

    def _extract_highlights(self, vespa_document_fields: Dict[str, Any]) -> List[Dict[Any, str]]:
        # For each tensor field we will have closest(tensor_field) and distance(tensor_field) in match features
        # If a tensor field hasn't been searched, closest(tensor_field)[cells] will be empty and distance(tensor_field)
        # will be max double
        match_features = vespa_document_fields[self._VESPA_DOC_MATCH_FEATURES]

        min_distance = None
        closest_tensor_field = None
        for tensor_field in self._marqo_index.tensor_fields:
            closest_feature = f'closest({tensor_field.embeddings_field_name})'
            if closest_feature in match_features and len(match_features[closest_feature]['cells']) > 0:
                distance_feature = f'distance(field,{tensor_field.embeddings_field_name})'
                if distance_feature not in match_features:
                    raise VespaDocumentParsingError(
                        f'Expected {distance_feature} in match features but it was not found'
                    )
                distance = match_features[distance_feature]
                if min_distance is None or distance < min_distance:
                    min_distance = distance
                    closest_tensor_field = tensor_field

        if closest_tensor_field is None:
            raise VespaDocumentParsingError('Failed to extract highlights from Vespa document. Could not find '
                                            'closest tensor field in response')

        # Get chunk index
        chunk_index_str = next(iter(
            match_features[f'closest({closest_tensor_field.embeddings_field_name})']['cells']
        ))
        try:
            chunk_index = int(chunk_index_str)
        except ValueError as e:
            raise VespaDocumentParsingError(
                f'Expected integer as chunk index, but found {chunk_index_str}', cause=e
            ) from e

        # Get chunk value
        try:
            chunk_field_name = closest_tensor_field.chunk_field_name

            if chunk_field_name in vespa_document_fields:
                chunk = vespa_document_fields[chunk_field_name][chunk_index]
            else:
                logger.warn(f'Failed to extract highlights as Vespa document is missing chunk field '
                            f'{chunk_field_name}. This can happen if attributes_to_retrieve does not include '
                            f'all searchable tensor fields (searchable_attributes)')

                chunk = None

        except (KeyError, TypeError, IndexError) as e:
            raise VespaDocumentParsingError(
                f'Cannot extract chunk value from {closest_tensor_field.chunk_field_name}: {str(e)}',
                cause=e
            ) from e

        if chunk:
            return [{closest_tensor_field.name: chunk}]
        else:
            return []

    def _get_python_type(self, marqo_type: FieldType) -> type:
        try:
            return self._MARQO_TO_PYTHON_TYPE_MAP[marqo_type]
        except KeyError:
            raise InternalError(f'Unknown Marqo type: {marqo_type}')
