import itertools
import os
import re
import warnings
from rasa_nlu import utils
from rasa_nlu.extractors import EntityExtractor
from rasa_nlu.train import create_argument_parser
from rasa_nlu.training_data.loading import _guess_format
from rasa_nlu.utils import write_json_to_file


COMPOSITE_ENTITIES_FILE_NAME = 'composite_entities.json'
RASA_NLU = 'rasa_nlu'


class CompositeEntityExtractor(EntityExtractor):
    """A component to add composite entities to Rasa NLU. Composite patterns
    can be defined in the normal rasa JSON train data file.
    """
    name = 'composite_entity_extractor'
    requires = ['entities']
    provides = ['composite_entities']

    def __init__(self, component_config=None, composite_entities=None):
        super().__init__(component_config)
        self.composite_entities = composite_entities or []

    @staticmethod
    def _read_composite_entities():
        """Read the defined composite patterns from the train file. We have
        to manually load the file, as rasa strips our custom information.
        """
        cmdline_args = create_argument_parser().parse_args()
        files = utils.list_files(cmdline_args.data)
        is_rasa_format = [_guess_format(file) == RASA_NLU for file in files]
        n_rasa_format = sum(is_rasa_format)
        # TODO: Support multiple training files
        assert sum(is_rasa_format) <= 1, 'Composite entities currently ' \
                'do not work with multiple training files.'
        if n_rasa_format == 1:
            file_index = [i for i, val in enumerate(is_rasa_format) if val][0]
            file = files[file_index]
            file_content = utils.read_json_file(file)
            rasa_nlu_data = file_content['rasa_nlu_data']
            composite_entities = rasa_nlu_data['composite_entities']
            return composite_entities

    def train(self, training_data, cfg, **kwargs):
        self.composite_entities = self._read_composite_entities()

    def process(self, message, **kwargs):
        self._find_composite_entities(message)

    def persist(self, model_dir):
        if self.composite_entities:
            composite_entities_file = os.path.join(
                    model_dir, COMPOSITE_ENTITIES_FILE_NAME)
            write_json_to_file(composite_entities_file, self.composite_entities,
                               separators=(',', ': '))

    @classmethod
    def load(cls, model_dir=None, model_metadata=None, cached_component=None,
             **kwargs):
        meta = model_metadata.for_component(cls.name)
        file_name = meta.get('composite_entities_file',
                             COMPOSITE_ENTITIES_FILE_NAME)
        composite_entities_file = os.path.join(model_dir, file_name)
        if os.path.isfile(composite_entities_file):
            composite_entities = utils.read_json_file(composite_entities_file)
        else:
            composite_entities = []
            warnings.warn('Failed to load composite entities file from '
                          f'"{composite_entities_file}"')
        return cls(meta, composite_entities)

    def _replace_entity_values(self, text, entities):
        """Replace entity values with their respective entity name."""
        new_text = ''
        index_map = []
        n_entities = len(entities)
        for i in range(n_entities):
            current_entity = entities[i]

            # If there is text before the first entity, include it as well
            if i == 0:
                new_text += text[:current_entity['start']]

            # Replace the entity value with its entity name. We store a mapping
            # from new positions to entity indices so that we can find entities
            # by refering to positions in this new string
            entity_start = len(new_text)
            new_text += '@' + current_entity['entity']
            index_map.append((i, entity_start, len(new_text)))

            # If there is text after the last entity, include it as well
            if i == n_entities - 1:
                new_text += text[current_entity['end']:]
            # Otherwise include the gap between this and the next entity
            else:
                next_entity = entities[i+1]
                new_text += text[current_entity['end']:next_entity['start']]
        return new_text, index_map

    def _find_composite_entities(self, message):
        """Find all composite entities in a message."""
        entities = message.get("entities", [])
        if not entities:
            return
        for entity in entities:
            entity['type'] = 'basic'

        text_with_entity_names, index_map = \
                self._replace_entity_values(message.text, entities)

        processed_composite_entities = []
        for composite_entity in self.composite_entities:
            contained_entity_indices = []
            # Sort patterns (longest pattern first) as longer patterns might
            # contain more information
            for pattern in sorted(composite_entity['patterns'], key=len, reverse=True):
                for match in re.finditer(pattern, text_with_entity_names):
                    contained_in_match = [
                        index for (index, start, end) in index_map
                        if start >= match.start() and end <= match.end()
                    ]
                    # If any entity for this match is already in the list, than
                    # this pattern is a subset of a previous (larger) pattern
                    # and we can ignore it.
                    all_indices = set(itertools.chain.from_iterable(
                            contained_entity_indices))
                    if all_indices & set(contained_in_match):
                        continue
                    contained_entity_indices.append(contained_in_match)
            if not contained_entity_indices:
                continue
            for contained_in_match in contained_entity_indices:
                contained_entities = list(sorted(
                        [entities[i] for i in contained_in_match],
                        key=lambda x: x['start']))
                processed_composite_entities.append({
                    'entity': composite_entity['name'],
                    'type': 'composite',
                    'contained_entities': contained_entities,
                })

        entities = [entity for i, entity in enumerate(entities) if i not in
                itertools.chain.from_iterable(contained_entity_indices)]
        message.set('entities', entities + processed_composite_entities,
                    add_to_output=True)
