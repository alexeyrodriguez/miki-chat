import os, re, argparse
from collections import namedtuple, OrderedDict, defaultdict
import yaml
import pandas as pd
import random
import logging
import io
import datetime

from nltk.stem import SnowballStemmer

import gspread
from gspread.models import Cell
from oauth2client.service_account import ServiceAccountCredentials

########################################
# Constants (templates or references to the question spreadsheet)
########################################

# The contexts for which questions and answers are retrieved
QNA_CONTEXTS = ['/bfz', '/specialitems']
CHITCHAT_CONTEXTS = ['/chitchat']

# The contexts for filter questions
FQ_CONTEXTS = ['/content']

# Sheets
SHEET_QUESTIONS = 'Fragenkatalog'
SHEET_FILTER_KEYWORDS = 'Schlüsselwörter'
SHEET_PHRASES = 'Imported Phrases'

# Columns used in the Questions Sheet
COL_CONTEXT = 'Context'
COL_INTENT = 'Intent'
COL_EXAMPLE = 'Ausgangsfrage / Beispiel'
COL_VARIANTS = 'Fragen (Varianten)'
COL_ANSWERS = ['Antwort_Part0 (Paraphrasing Question)'] + [f'Antwort_Part{i+1}' for i in range(3)] + ['Link 1']

# Columns used in the filter keywords sheet
COL_FILTER_CONTEXT = 'Context'
COL_KEY = 'Key'
COL_FILTER = 'Filter ID'
COL_KEYWORD = 'Schlüsselwörter'
COL_SYNONYM = 'Synonym _NUMBER_'
NUM_SYNONYMS = 15

# Columns used in the phrases sheet
COL_PHRASE_KEY = 'Key'
COL_PHRASE_ANSWERS = [f'Answer {i+1}' for i in range(5)]

# Utterances with yes/no answers
YESNO_PHRASES = '/ask_whether_question_answered'

# Automatic example generation, generate examples for the following contexts
AUTO_GENERATE_FOR_CONTEXTS =  ['_quarter', '_language', '_targetgroup', '_topic']

########################################
# Arguments and logging
########################################

def get_args():
    parser = argparse.ArgumentParser(description="Import Question and Answer examples from BfZ spreadsheet")
    parser.add_argument('--client-secret', type=str, help='Path to json key file of service account', required=True)
    parser.add_argument('--spreadsheet-url', type=str, help='URL of Google Spreadsheet containing Questions and Answers', required=True)
    parser.add_argument('--output-dir', type=str, help='Directory name where output will be saved', required=True)
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--detailed-logging', action='store_true')
    parser.add_argument('--save-logs-to-spreadsheet', help='Save logs to spreadsheet, make sure there are Sheets named "Logs" and "Logs Detailed"', action='store_true')
    return parser.parse_args()

args = get_args()

# Summary logger
sum_logger = logging.getLogger('import.summary')
sum_logger.setLevel(logging.INFO)
summary_stream = io.StringIO()
sum_logger.addHandler(logging.StreamHandler(summary_stream))

logger = logging.getLogger('import.detailed')
logger.setLevel(logging.INFO)
detailed_stream = io.StringIO()
logger.addHandler(logging.StreamHandler(detailed_stream))

if not args.quiet:
    str_handler = logging.StreamHandler()
    str_handler.setLevel(logging.INFO)
    sum_logger.addHandler(str_handler)

if not args.quiet and args.detailed_logging:
    str_handler = logging.StreamHandler()
    str_handler.setLevel(logging.INFO)
    logger.addHandler(str_handler)

sum_logger.info(f'Starting Question Import {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
logger.info(f'Starting Question Import {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')


def main():
    spreadsheet = open_spreadsheet(args)

    # Create directory structure
    os.makedirs(f'{args.output_dir}/data/generated/faq', exist_ok=True)
    os.makedirs(f'{args.output_dir}/data/generated/chitchat', exist_ok=True)
    os.makedirs(f'{args.output_dir}/data/generated/filter_questions/entities', exist_ok=True)

    # Dump FAQ NLU data
    question_rows = get_question_sheet(spreadsheet)
    with open(f'{args.output_dir}/data/generated/faq/nlu.yml', 'w') as f:
        faq = questions_answers_nlu_data(QNA_CONTEXTS, question_rows, 'faq')
        f.write(yaml.dump(faq, allow_unicode=True))

    question_rows = get_question_sheet(spreadsheet)
    with open(f'{args.output_dir}/data/generated/chitchat/nlu.yml', 'w') as f:
        faq = questions_answers_nlu_data(CHITCHAT_CONTEXTS, question_rows, 'chitchat')
        f.write(yaml.dump(faq, allow_unicode=True))

    filter_rows = get_filter_keyword_sheet(spreadsheet)
    filter_rows = filter_keywords(args, filter_rows)

    filters_df(filter_rows).to_csv(f'{args.output_dir}/data/generated/filter_questions/entities/filter_mapping.csv', index=False)
    synonyms_df(filter_rows).to_csv(f'{args.output_dir}/data/generated/filter_questions/entities/filter_synonyms.csv', index=False)

    with open(f'{args.output_dir}/data/generated/filter_questions/entities/nlu.yml', 'w') as f:
        nlu = filters_nlu_data(filter_rows)
        f.write(yaml.dump(nlu, allow_unicode=True))

    synonyms = make_synonyms(filter_rows)
    qs = filter_questions_nlu_data(question_rows, synonyms)
    new_qs = generate_examples(qs, synonyms, filter_rows)
    log_synonyms_without_examples(qs + new_qs, filter_rows)
    log_invalid_questions(qs)
    log_generated_questions(new_qs)
    nlu = filter_questions_yaml(qs + new_qs)

    with open(f'{args.output_dir}/data/generated/filter_questions/nlu.yml', 'w') as f:
        f.write(yaml.dump(nlu, allow_unicode=True))

    phrases = get_phrases(spreadsheet)
    with open(f'{args.output_dir}/data/generated/domain.yml', 'w') as f:
        nlu = phrase_utterances(phrases)
        f.write(yaml.dump(nlu, allow_unicode=True))

    if args.save_logs_to_spreadsheet:
        sum_logger.info('End of import\n')
        logger.info('End of import\n')
        save_logs_to_sheet(spreadsheet, 'Logs', summary_stream.getvalue())
        save_logs_to_sheet(spreadsheet, 'Logs Detailed', detailed_stream.getvalue())


def make_synonyms(filter_rows):
    Synonym = namedtuple('Synonym', 'syn filter num_examples')
    synonyms = [Synonym(syn, row.filter, 0) for row in filter_rows for syn in [row.keyword] + row.synonyms]
    return synonyms



def log_synonyms_without_examples(qs, filter_rows):
    contexts = set([f.context for f in filter_rows])
    for c in contexts:
        fs = [f for f in filter_rows if f.context==c]

        filters_without_examples = []
        for f in fs:
            examples = [q for q in qs if q.is_valid and f.keyword in q.entities + q.auto_entities]
            if not examples:
                filters_without_examples.append(f.keyword)

        if filters_without_examples:
            sum_logger.info(f'Synonyms, context {c}, WARNING there are {len(filters_without_examples)} filters without examples')
            logger.info(f'Synonyms, context {c}, WARNING neither the following filters or the corresponding synonyms have no examples: {filters_without_examples}')


def log_generated_questions(qs):
    for q in qs:
        logger.info(f'Generated question: {q.question}')



def generate_examples(qs, synonyms, filter_rows):

    def quote_star(s):
        return s.replace('*', r'\*')

    syn_to_filter = {s: f for f in filter_rows for s in [f.keyword] + f.synonyms}
    ctx_to_filter = defaultdict(list)
    for f in filter_rows:
        ctx_to_filter[f.context] += [f]

    q_dict = defaultdict(list)
    ctx_dict = {}
    for q in qs:
        if q.is_valid:
            for s in q.entities + q.auto_entities:
                q_dict[s] += [q]

    qs = []
    generated = []
    for s in synonyms:
        filter = syn_to_filter[s.syn]

        if not s.syn in q_dict:
            syns = [filter.keyword] + filter.synonyms
            random.shuffle(syns)

            example = None

            for syn in syns:
                if syn in q_dict:
                    q = random.choice(q_dict[syn])
                    question = re.sub(f'\[{quote_star(syn)}\]', f'[{s.syn}]', q.question)
                    example = (syn, q._replace(question=question))
                    break

            # If still no examples, fallback to context
            if filter.context in AUTO_GENERATE_FOR_CONTEXTS:
                for f in ctx_to_filter[filter.context]:
                    for syn in [f.keyword] + f.synonyms:
                        if syn in q_dict:
                            q = random.choice(q_dict[syn])
                            question = re.sub(f'\[{quote_star(syn)}\]', f'[{s.syn}]', q.question)
                            example = (syn, q._replace(question=question, auto_entities=q.auto_entities+[s.syn]))
                            break
                    if example:
                        break

            if example:
                qs.append(example[1])
                generated.append((s, filter))

    contexts = set([f.context for _, f in generated])
    for c in contexts:
        gens = [g for g in generated if g[1].context==c]
        sum_logger.info(f'Example generation, context {c}, examples generated for {len(gens)} synonyms')
        logger.info(f'Example generation, context {c}, examples generated for {[s.syn for s, _ in gens]}')

    return qs


def save_logs_to_sheet(spreadsheet, sheetname, logs):
    worksheet = spreadsheet.worksheet(sheetname)
    lines = logs.split('\n')
    cells = [Cell(1+number,1, line) for line, number in zip(lines, range(len(lines)))]
    worksheet.update_cells(cells)


#################
# Spreadsheet processing logic
#################

Question = namedtuple('Question', 'context intent question question_variants answers')


def filter_keywords(args, filter_rows):
    gs = group_by_column(filter_rows, 'context')

    filters_with_key = [row._replace(key=rows[0].key, context=context, valid=rows[0].key and context)
                        for context, rows in gs.items()
                        for row in rows[1:]
                        if row.keyword] # We remove cosmetic empty rows

    # Logging
    contexts = set([f.context for f in filters_with_key])
    for c in contexts:
        fs = [f for f in filters_with_key if f.context==c]
        invalid = [f for f in fs if not f.valid]

        sum_logger.info(f'Synonyms, context {c}: Reading {len(fs)} synonyms')
        all_syns = [s for f in fs if f.valid for s in [f.keyword] + f.synonyms]
        logger.info(f'Synonyms, context {c}: Reading keyword and synonym variants: {all_syns}')

        overlapped_keywords = 0
        # Expensive overlap check
        for f in fs:
            syns = [f.keyword] + f.synonyms
            other_syns = [syn
                          for of in filters_with_key if of.valid and of.keyword!=f.keyword
                          for syn in [of.keyword] + of.synonyms]
            overlap = [s for s in syns if s in other_syns]
            if overlap:
                logger.info(f'Synonyms, context {c}, keyword {f.keyword}, found conflicting synonyms: {overlap}, please solve the conflicts in the filter keywords sheet')
                overlapped_keywords += 1


        if invalid:
            sum_logger.info(f'Synonyms, context {c}: WARNING discarding {len(invalid)} filter keywords')
            logger.info(f'Synonyms, context {c}: Discarding filter keywords {[f.keyword for f in invalid]} because of missing key or filter')

        if overlapped_keywords:
            sum_logger.info(f'Synonyms, context {c}: WARNING found {overlapped_keywords} filter keywords with conflicts, see detailed log and resolve the conflicts')

    filters_with_key_and_filter = [row for row in filters_with_key if row.filter and row.key]

    return filters_with_key_and_filter

# CONSTANT
def filters_df(filter_rows):
    stemmer = SnowballStemmer('german')
    stemmed_filters = [r.keyword for r in filter_rows]
    return pd.DataFrame({
        'filter': [r.filter for r in filter_rows],
        'display': [r.keyword for r in filter_rows],
        'filter_category': [r.key for r in filter_rows],
        'context': [r.context for r in filter_rows],
        'is_search_term': [r.context=='_searchterms' for r in filter_rows],
        'stemmed': [r.context=='_searchterms' for r in filter_rows],
    }).sort_values('filter')

def synonyms_df(filter_rows):
    stemmer = SnowballStemmer('german')
    syns = [(stemmer.stem(s), r.filter) for r in filter_rows for s in [r.keyword] + r.synonyms]
    return pd.DataFrame({
        'synonym': [s for s, _ in syns],
        'filter': [f for _, f in syns],
    }).sort_values('filter')

def filters_nlu_data(filter_rows):
    return OrderedDict({
        'version': '2.0',
        'nlu':
            [OrderedDict(
                {
                    'synonym': r.filter,
                    'examples': format_examples([r.keyword] + r.synonyms)
                }
             )
             for r in filter_rows]
    })


def create_responses(answers):
    return '\n\n'.join(answers)


def questions_answers_nlu_data(contexts, question_rows, main_intent):
    gs = group_by_column(question_rows, 'context')
    qa_rows = [row._replace(context=context) for context in contexts if context in gs for row in gs[context]]
    bfz_questions = group_by_column(qa_rows, 'intent')

    questions = [Question(rows[0].context,
                          f'{rows[0].context[1:]}_{intent[1:]}',
                          rows[0].question,
                          [r.question_variant for r in rows if r.question_variant != ''],
                          [r.answers for r in rows if r.answers])
                 for intent, rows in bfz_questions.items()]

    for context in contexts:
        qs = [q for q in questions if q.context==context]
        discarded = [q for q in qs if not q.question]
        sum_logger.info(f'Questions and Answers, context {context}: Importing {len(qs)} questions and answers, discarding {len(discarded)}')

    questions = [q for q in questions if q.question]

    # Create faq yaml
    faq = OrderedDict({
        'version': '2.0',
        'nlu':
            [OrderedDict(
                {'intent': f'{main_intent}/{q.intent}',
                 'examples': format_examples([q.question] + [v for v in q.question_variants])})
                for q in questions],
        'responses':
            OrderedDict(
                {f'utter_{main_intent}/{q.intent}': [{'text': create_responses(answers)} for answers in q.answers]
                 for q in questions})
    })

    return faq

TaggedQuestion = namedtuple('TaggedQuestion', 'question entities auto_entities invalid_entities is_valid reason_invalid')

def process_question(synonyms, question):
    # Find all text marked by square braces
    tagged_entities = re.findall(r'\[[^\[]*\]', question)
    tagged_entities = [t[1:-1] for t in tagged_entities]
    tagged_entities = set(tagged_entities)

    invalid_entities = list(tagged_entities - synonyms)

    remaining_synonyms = list(synonyms - tagged_entities)

    new_entities = []
    tagged_entities = list(tagged_entities)

    # Order remaining synonyms by larger to smaller to avoid accidental matches
    remaining_synonyms = sorted([(len(s), s) for s in remaining_synonyms], reverse=True)
    remaining_synonyms = [s[1] for s in remaining_synonyms]

    for syn in remaining_synonyms:
        if not [syn in s for s in new_entities + tagged_entities]:
            # Make sure that analyzed new synonym is not a substring of an existing tag
            i = question.find(syn)
            if i>=0 and (i+len(syn) >= len(question) or question[i+len(syn)] in ' .,?'):
                new_entities.append(syn)
                question = question[:i] + '[' + syn + ']' + question[i+len(syn):]

    if invalid_entities:
        reason_invalid = 'Invalid filter keywords found'
    elif not (new_entities + tagged_entities):
        reason_invalid = 'No filter keywords found'
    else:
        reason_invalid = ''

    return TaggedQuestion(question, tagged_entities, new_entities, invalid_entities, not reason_invalid, reason_invalid)

def filter_questions_nlu_data(question_rows, synonyms):

    synonyms = set([s.syn for s in synonyms])
    gs = group_by_column(question_rows, 'context')
    content_questions = [row for context in FQ_CONTEXTS if context in gs for row in gs[context]]

    qs = [process_question(synonyms, r.question_variant) for r in content_questions if r.question_variant]

    vqs = [q for q in qs if q.is_valid]

    sum_logger.info(f'Filter questions, reading {len(qs)} questions, discarding {len(qs) - len(vqs)}')

    return qs


def log_invalid_questions(qs):
    for q in qs:
        if not q.is_valid:
            if q.invalid_entities:
                invalid_entities = f'Invalid keywords: {q.invalid_entities}'
            else:
                invalid_entities = ''
            logger.info(f'Filter questions, discarding question {q.question} because {q.reason_invalid}. {invalid_entities}')

def filter_questions_yaml(qs):

    def rasa_tagging(s):
        '''Replaces, appends all entities tagged with a square bracket with a annotation (filter)'''
        return re.sub(r'\[([^\[]*)\]', '[\\1](filter)', s)

    return OrderedDict({
        'version': '2.0',
        'nlu':
            [OrderedDict(
                {
                    'intent': 'filter_question',
                    'examples': format_examples([rasa_tagging(q.question) for q in qs if q.is_valid])
                }
            )]
    })


def phrase_utterances(phrases):
    def gen_response(p):
        res = []
        for answers in p.answers:
            d = OrderedDict()
            d['text'] = create_responses(answers)
            if p.key in YESNO_PHRASES:
                d['buttons'] = [OrderedDict({'title': 'Ja', 'payload': '/affirm'}),
                                OrderedDict({'title': 'Nein', 'payload': '/deny'})]
            res.append(d)
        return res

    return OrderedDict({
        'version': '2.0',
        'responses':
            OrderedDict(
                {f'utter_{p.key}': gen_response(p)
                 for p in phrases})
    })


def format_examples(qs):
    return '\n'.join([f'- {q}' for q in qs if q])


def group_by_column(rows, col):
    groups = OrderedDict()
    current_val = None
    current = []
    for r in rows:
        if getattr(r, col)!='':
            if current_val:
                groups[current_val] = current
            current_val = getattr(r, col)
            current = [r]
        else:
            current.append(r)
    if current_val:
        groups[current_val] = current
    return groups

#################
# Utility Functions
#################


def open_spreadsheet(args):
    scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name(args.client_secret, scope)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_url(args.spreadsheet_url)
    return spreadsheet


def get_question_sheet(spreadsheet):

    # Raw rows of the question spreadsheet
    Row = namedtuple('Row', 'context intent question question_variant answers')

    sheet = spreadsheet.worksheet(SHEET_QUESTIONS)
    list_of_hashes = sheet.get_all_records()

    rows = [Row(r[COL_CONTEXT], r[COL_INTENT], r[COL_EXAMPLE], r[COL_VARIANTS],
                [r[col_answer] for col_answer in COL_ANSWERS if r[col_answer]])
            for r in list_of_hashes]
    return rows


def get_filter_keyword_sheet(spreadsheet):

    # Raw rows of the question spreadsheet
    Row = namedtuple('Row', 'context key filter keyword synonyms valid')

    sheet = spreadsheet.worksheet(SHEET_FILTER_KEYWORDS)
    list_of_hashes = sheet.get_all_records()

    def syn(r, i):
        key = COL_SYNONYM.replace('_NUMBER_', str(i))
        if key in r:
            return r[key]
        else:
            return ''

    def clean(s):
        return s.strip().replace('[','').replace(']','')

    rows = [Row(r[COL_FILTER_CONTEXT], clean(r[COL_KEY]), clean(r[COL_FILTER]), clean(r[COL_KEYWORD]),
                [clean(syn(r, i)) for i in range(1, NUM_SYNONYMS) if clean(syn(r, i))], True)
            for r in list_of_hashes]
    return rows


def get_phrases(spreadsheet):

    # Raw rows of the question spreadsheet
    Phrase = namedtuple('Phrase', 'key answers')

    sheet = spreadsheet.worksheet(SHEET_PHRASES)
    list_of_hashes = sheet.get_all_records()

    phrases = [Phrase(r[COL_PHRASE_KEY],
                [r[col_answer].strip() for col_answer in COL_PHRASE_ANSWERS if r[col_answer].strip()])
            for r in list_of_hashes]

    # Post process to group variants per key
    # There is a change of type in answers where there is a list of lists
    # The outer list represent the variants, the inner list represents the bubbles (multiple subsequent answers)
    gs = group_by_column(phrases, 'key')
    phrases = [Phrase(key[1:], [phrase.answers for phrase in phrases if phrase.answers])
        for key, phrases in gs.items()
        if key[0]=='/'
    ]

    sum_logger.info(f'Phrases, Importing {len(phrases)} phrases')

    return phrases


#################
# YAML rendering setup
#################

def str_presenter(dumper, data):
    if len(data.splitlines()) > 1:  # check for multiline string
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
    return dumper.represent_scalar('tag:yaml.org,2002:str', data)

def ordered_dict_presenter(dumper, data):
    return dumper.represent_dict(data.items())
yaml.add_representer(OrderedDict, ordered_dict_presenter)
yaml.add_representer(str, str_presenter)


# Run the main function
main()


