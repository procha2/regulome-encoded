import pickle
from pkg_resources import resource_filename

from operator import itemgetter

from elasticsearch.exceptions import (
    NotFoundError
)
from snovault.elasticsearch.indexer_state import SEARCH_MAX

from .regulome_indexer import (
    snp_index_key,
    RESIDENT_REGIONSET_KEY,
    FOR_REGULOME_DB,
    REGULOME_ALLOWED_STATUSES,
    REGULOME_DATASET_TYPES
)

# ##################################
# RegionAtlas and RegulomeAtlas classes encapsulate the methods
# for querying regions and SNPs from the region_index.
# ##################################

# when iterating scored snps or bases, chunk calls to index for efficiency
# NOTE: failures seen when chunking is too large
REGDB_SCORE_CHUNK_SIZE = 30000

# RegulomeDB scores for bigWig (bedGraph) are converted to numeric and can be converted back
REGDB_STR_SCORES = ['1a', '1b', '1c', '1d', '1e', '1f', '2a', '2b', '2c', '3a', '3b', '4', '5', '6']
REGDB_NUM_SCORES = [1000, 950, 900, 850, 800, 750, 600, 550, 500, 450, 400, 300, 200, 100]

# def includeme(config):
#    config.scan(__name__)
#    registry = config.registry
#    registry['region'+INDEXER] = RegionIndexer(registry)

# Make prediction on query data with trained random forest model load trained model
TRAINED_REG_MODEL = pickle.load(
    open(resource_filename('encoded', 'rf_model.sav'), 'rb')
)


class RegulomeAtlas(object):
    '''Methods for getting stuff out of the region_index.'''

    def __init__(self, region_es):
        self.region_es = region_es

    def type(self):
        return 'regulome'

    def allowed_statuses(self):
        return REGULOME_ALLOWED_STATUSES

    def set_type(self):
        return ['Dataset']

    def set_indices(self):
        indices = [set_type.lower() for set_type in REGULOME_DATASET_TYPES]
        return indices

    # def snp_suggest(self, assembly, text):
    # Using suggest with 60M of rsids leads to es crashing during SNP indexing

    def snp(self, assembly, rsid):
        '''Return single SNP by rsid and assembly'''
        try:
            res = self.region_es.get(index=snp_index_key(assembly), doc_type='_all', id=rsid)
            #TODO _all is deprecated
        except Exception:
            return None

        return res['_source']

    @staticmethod
    def _range_query(start, end, snps=False, with_inner_hits=False, max_results=SEARCH_MAX):
        '''private: return peak query'''
        # get all peaks that overlap requested point
        # only single point intersection
        # use start not end for 0-base open ended
        if abs(int(end) - int(start)) == 1:
            query = {
                'query': {
                    'term': {
                        'coordinates': start
                    }
                },
                'size': max_results,
            }
        else:
            query = {
                'query': {
                    'range': {
                        'coordinates': {
                            'gte': start,
                            'lt': end,
                            'relation': 'intersect',
                        }
                    }
                },
                'size': max_results,
            }


        return query

    def find_snps(self, assembly, chrom, start, end, max_results=SEARCH_MAX):
        '''Return all SNPs in a region.'''
        range_query = self._range_query(start, end, snps=True)

        try:
            results = self.region_es.search(index=snp_index_key(assembly), doc_type=chrom,
                                            _source=True, body=range_query, size=max_results)
        except NotFoundError:
            return []
        except Exception:
            return []

        return [hit['_source'] for hit in results['hits']['hits']]

    # def snp_suggest(self, assembly, text):
    # Using suggest with 60M of rsids leads to es crashing during SNP indexing

    def find_peaks(self, assembly, chrom, start, end, peaks_too=False, max_results=SEARCH_MAX):
        '''Return all peaks intersecting a point'''
        range_query = self._range_query(start, end, False, peaks_too, max_results)

        try:
            results = self.region_es.search(index=chrom.lower(), doc_type=assembly, _source=True,
                                            body=range_query, size=max_results)
        except NotFoundError:
            return None
        except Exception:
            return None

        return list(results['hits']['hits'])

    def _resident_details(self, uuids, max_results=SEARCH_MAX):
        '''private: returns resident details filtered by use.'''
        try:
            id_query = {"query": {"ids": {"values": uuids}}}
            res = self.region_es.search(index=RESIDENT_REGIONSET_KEY, body=id_query,
                                        doc_type=[FOR_REGULOME_DB], size=max_results)
        except Exception:
            return None
        details = {}
        hits = res.get("hits", {}).get("hits", [])
        for hit in hits:
            details[hit["_source"]["uuid"]] = hit["_source"]

        return details

    def find_peaks_filtered(self, assembly, chrom, start, end, peaks_too=False):
        '''Return peaks in a region and resident details'''
        #TODO I don't know why this also returns details it's not ever used productively
        peaks = self.find_peaks(assembly, chrom, start, end, peaks_too=peaks_too)
        if not peaks:
            return (peaks, None)
        uuids = list(set([peak['_source']['uuid'] for peak in peaks]))
        details = self._resident_details(uuids)
        if not details:
            return ([], details)
        filtered_peaks = []
        while peaks:
            peak = peaks.pop(0)
            uuid = peak['_source']['uuid']
            if uuid in details:
                peak['resident_detail'] = details[uuid]
                filtered_peaks.append(peak)
        return (filtered_peaks, details)

    @staticmethod
    def _peak_uuids_in_overlap(peaks, chrom, start, end=None):
        '''private: returns set of only the uuids for peaks that overlap a given location'''
        if end is None:
            end = start

        overlap = set()
        for peak in peaks:
            if chrom == peak['_index'] and \
                    start <= peak['_source']['coordinates']['lt'] and \
                    end >= peak['_source']['coordinates']['gte']:
                overlap.add(peak['_source']['uuid'])

        return overlap

    @staticmethod
    def _filter_details(details, uuids=None, peaks=None):
        '''private: returns only the details that match the uuids'''
        if uuids is None:
            assert(peaks is not None)
            uuids = list(set([peak['_source']['uuid'] for peak in peaks]))
        filtered = {}
        for uuid in uuids:
            if uuid in details:  # region peaks may not be in regulome only details
                filtered[uuid] = details[uuid]
        return filtered

    @staticmethod
    def details_breakdown(details, uuids=None):
        '''Return dataset and file dicts from resident details dicts.'''
        if not details:
            return (None, None)
        file_dets = {}
        dataset_dets = {}
        if uuids is None:
            uuids = details.keys()
        for uuid in uuids:
            if uuid not in details:
                continue
            afile = details[uuid]['file']
            file_dets[afile['@id']] = afile
            dataset = details[uuid]['dataset']
            dataset_dets[dataset['@id']] = dataset
        return (dataset_dets, file_dets)

    @staticmethod
    def evidence_categories():
        '''Returns a list of regulome evidence categories'''
        return ['eQTL', 'ChIP', 'DNase', 'PWM', 'Footprint', 'PWM_matched', 'Footprint_matched']

    @staticmethod
    def _score_category(dataset):
        '''private: returns one of the categories of evidence that are needed for scoring.'''
        # score categories are slighly different from regulome categories
        collection_type = dataset.get('collection_type')  # resident_regionset dataset
        if collection_type in ['ChIP-seq', 'binding sites']:
            return 'ChIP'
        if collection_type == 'DNase-seq':
            return 'DNase'
        if collection_type == 'PWMs':
            return 'PWM'
        if collection_type == 'Footprints':
            return 'Footprint'
        if collection_type in ['eQTLs', 'curated SNVs']:
            return 'eQTL'
        if collection_type == 'dsQTLs':
            return 'dsQTL'
        return None

    def _regulome_category(self, score_category=None, dataset=None):
        '''private: returns one of the categories used to present evidence in a bed file.'''
        # regulome category 'Motifs' contains score categories 'PWM' and 'Footprint'
        if score_category is None:
            if dataset is None:
                return '???'
            score_category = self._score_category(dataset)
        if score_category == 'ChIP':
            return 'Protein_Binding'
        if score_category == 'DNase':
            return 'Chromatin_Structure'
        if score_category in ['PWM', 'Footprint']:
            return 'Motifs'
        if score_category == 'eQTL':
            return 'Single_Nucleotides'
        return '???'

    def regulome_evidence(self, datasets):
        '''Returns evidence for scoring: datasets in a characterized dict'''
        evidence = {}
        targets = {'ChIP': [], 'PWM': [], 'Footprint': []}
        for dataset in datasets.values():
            character = self._score_category(dataset)
            if character is None:
                continue
            if character not in evidence:
                evidence[character] = []
            evidence[character].append(dataset)
            target = dataset.get('target')
            if target and character in ['ChIP', 'PWM', 'Footprint']:
                if isinstance(target, str):
                    targets[character].append(target)
                elif isinstance(target, list):  # rare but PWM targets might be list
                    for targ in target:
                        targets[character].append(targ)

        # Targets... For each ChIP target, there could be a PWM and/or Footprint to match
        for target in targets['ChIP']:
            if target in targets['PWM']:
                if 'PWM_matched' not in evidence:
                    evidence['PWM_matched'] = []
                evidence['PWM_matched'].append(target)
            if target in targets['Footprint']:
                if 'Footprint_matched' not in evidence:
                    evidence['Footprint_matched'] = []
                evidence['Footprint_matched'].append(target)

        return evidence

    def _write_a_brief(self, category, snp_evidence):
        '''private: given evidence for a category make a string that summarizes it'''
        snp_evidence_category = snp_evidence[category]

        # What do we want the brief to look like?
        # Regulome: Chromatin_Structure|DNase-seq|Chorion|,
        #           Chromatin_Structure|DNase-seq|Adultcd4th1|,
        #           Protein_Binding|ChIP-seq|E2F1|MCF-7|, ...
        # Us: Chromatin_Structure:DNase-seq:|ENCSR...|Chorion|,|ENCSR...|Adultcd4th1| (tab)
        #           Protein_Binding/ChIP-seq:|ENCSR...|E2F1|MCF-7|,|ENCSR...|SP4|H1-hESC|
        brief = ''
        cur_score_category = ''
        cur_regdb_category = ''
        for dataset in snp_evidence_category:
            new_score_category = self._score_category(dataset)
            if cur_score_category != new_score_category:
                cur_score_category = new_score_category
                new_regdb_category = self._regulome_category(cur_score_category)
                if cur_regdb_category != new_regdb_category:
                    cur_regdb_category = new_regdb_category
                    if brief != '':  # 'PWM' and 'Footprint' are both 'Motif'
                        brief += ';'
                    brief += '%s:' % cur_regdb_category
                brief += '%s:|' % cur_score_category
            try:
                brief += dataset.get('@id', '').split('/')[-2] + '|'  # accession is buried in @id
            except Exception:
                brief += '|'
            target = dataset.get('target')
            if target:
                if isinstance(target, list):
                    target = '/'.join(target)
                brief += target.replace(' ', '') + '|'
            biosample = dataset.get('biosample_term_name', dataset.get('biosample_summary'))
            if biosample:
                brief += biosample.replace(' ', '') + '|'
            brief += ','
        return brief[:-1]   # remove last comma

    def make_a_case(self, snp):
        '''Convert evidence json to list of evidence strings for bed batch downloads.'''
        case = {}
        if 'evidence' in snp:
            for category in snp['evidence'].keys():
                if category.endswith('_matched'):
                    case[category] = ','.join(snp['evidence'][category])
                else:
                    case[category] = self._write_a_brief(category, snp['evidence'])
        return case

    @staticmethod
    def _score(characterization):
        '''private: returns regulome score from characterization set'''
        # Predict as probability of being a regulatory SNP from prediction
        keys = ['ChIP', 'DNase', 'PWM', 'Footprint', 'eQTL', 'dsQTL',
                'PWM_matched', 'Footprint_matched']
        query = [[int(k in characterization) for k in keys]]
        probability = str(round(TRAINED_REG_MODEL.predict_proba(query)[:, 1][0], 5))
        ranking = '7'
        if ('eQTL' in characterization) or ('dsQTL' in characterization):
            if 'ChIP' in characterization:
                if 'DNase' in characterization:
                    if 'PWM_matched' in characterization and 'Footprint_matched' in characterization:
                        ranking = '1a'
                    elif 'PWM' in characterization and 'Footprint' in characterization:
                        ranking = '1b'
                    elif 'PWM_matched' in characterization:
                        ranking = '1c'
                    elif 'PWM' in characterization:
                        ranking = '1d'
                    else:
                        ranking = '1f'
                elif 'PWM_matched' in characterization:
                    ranking = '1e'
                else:
                    ranking = '1f'
            elif 'DNase' in characterization:
                ranking = '1f'
            elif 'PWM' in characterization or 'Footprint' in characterization:
                ranking = '6'
        elif 'ChIP' in characterization:
            if 'DNase' in characterization:
                if 'PWM_matched' in characterization and 'Footprint_matched' in characterization:
                    ranking = '2a'
                elif 'PWM' in characterization and 'Footprint' in characterization:
                    ranking = '2b'
                elif 'PWM_matched' in characterization:
                    ranking = '2c'
                elif 'PWM' in characterization:
                    ranking = '3a'
                else:
                    ranking = '4'
            elif 'PWM_matched' in characterization:
                ranking = '3b'
            else:
                ranking = '5'
        elif 'DNase' in characterization:
            ranking = '5'
        elif ('PWM' in characterization
              or 'Footprint' in characterization):
            ranking = '6'
        return '{} (probability); {} (ranking v1.1)'.format(probability, ranking)

    def regulome_score(self, datasets, evidence=None):
        '''Calculate RegulomeDB score based upon hits and voodoo'''
        if not evidence:
            evidence = self.regulome_evidence(datasets)
            if not evidence:
                return None
        return self._score(set(evidence.keys()))

    @staticmethod
    def _snp_window(snps, window, center_pos=None):
        '''Reduce a list of snps to a set number of snps centered around position'''
        if len(snps) <= window:
            return snps

        snps = sorted(snps, key=lambda s: s['coordinates']['gte'])
        ix = 0
        for snp in snps:
            if snp['coordinates']['gte'] >= center_pos:
                break
            ix += 1

        first_ix = int(ix - (window / 2))
        if first_ix > 0:
            snps = snps[first_ix:]
        return snps[:window]

    def _scored_snps(self, assembly, chrom, start, end, window=-1, center_pos=None):
        '''For a region, yields all SNPs with scores'''
        snps = self.find_snps(assembly, chrom, start, end)
        if not snps:
            return
        if window > 0:
            snps = self._snp_window(snps, window, center_pos)

        start = snps[0]['coordinates']['gte']  # SNPs must be in location order!
        end = snps[-1]['coordinates']['lt']                                        # MUST do SLOW peaks_too
        (peaks, details) = self.find_peaks_filtered(assembly, chrom, start, end, peaks_too=True)
        if not peaks or not details:
            for snp in snps:
                snp['score'] = None
                yield snp
                return

        last_uuids = {}
        last_snp = {}
        for snp in snps:
            snp['score'] = None  # default
            snp['assembly'] = assembly
            snp_uuids = self._peak_uuids_in_overlap(peaks, snp['chrom'], snp['coordinates']['gte'])
            if snp_uuids:
                if snp_uuids == last_uuids:  # good chance evidence hasn't changed
                    if last_snp:
                        snp['score'] = last_snp['score']
                        if 'evidence' in last_snp:
                            snp['evidence'] = last_snp['evidence']
                    yield snp
                    continue
                else:
                    last_uuids = snp_uuids
                    snp_details = self._filter_details(details, uuids=list(snp_uuids))
                    if snp_details:
                        (snp_datasets, _snp_files) = self.details_breakdown(snp_details)
                        if snp_datasets:
                            snp_evidence = self.regulome_evidence(snp_datasets)
                            if snp_evidence:
                                snp['score'] = self.regulome_score(snp_datasets, snp_evidence)
                                snp['evidence'] = snp_evidence
                                # snp['datasets'] = snp_datasets
                                # snp['files'] = snp_files
                                last_snp = snp
                                yield snp
                                continue
            # if we are here this snp had no score
            last_snp = {}
            yield snp

    def _scored_regions(self, assembly, chrom, start, end):
        '''For a region, yields sub-regions (start, end, score) of contiguous numeric score > 0'''
        (peaks, details) = self.find_peaks_filtered(assembly, chrom, start, end, peaks_too=True)
        if not peaks or not details:
            return

        last_uuids = set()
        region_start = 0
        region_end = 0
        region_score = 0
        num_score = 0
        for base in range(start, end):
            base_uuids = self._peak_uuids_in_overlap(peaks, chrom, base)
            if base_uuids:
                if base_uuids == last_uuids:
                    region_end = base  # extend region
                    continue
                else:
                    last_uuids = base_uuids
                    base_details = self._filter_details(details, uuids=list(base_uuids))
                    if base_details:
                        (base_datasets, _base_files) = self.details_breakdown(base_details)
                        if base_datasets:
                            base_evidence = self.regulome_evidence(base_datasets)
                            if base_evidence:
                                score = self.regulome_score(base_datasets, base_evidence)
                                if score:
                                    num_score = self.numeric_score(score)
                                    if num_score == region_score:
                                        region_end = base
                                        continue
                                    if region_score > 0:  # end previous region?
                                        yield (region_start, region_end, region_score)
                                    # start new region
                                    region_score = num_score
                                    region_start = base
                                    region_end = base
                                    continue
            # if we are here this base had no score
            if region_score > 0:  # end previous region?
                yield (region_start, region_end, region_score)
                region_score = 0
                last_uuids = base_uuids  # zero score so don't try these uuids again!

        if region_score > 0:  # end previous region?
            yield (region_start, region_end, region_score)

    def nearby_snps(self, assembly, chrom, pos, rsid=None, window=1600,
                    max_snps=10, scores=False):
        '''Return SNPs nearby to the chosen SNP.'''
        if rsid:
            max_snps += 1

        range_start = int(pos - (window / 2))
        range_end = int(pos + (window / 2))
        if range_start < 0:
            range_end += 0 - range_start
            range_start = 0

        if scores:
            snps = self._scored_snps(assembly, chrom, range_start, range_end)
        else:
            snps = self.find_snps(assembly, chrom, range_start, range_end)
            snps = self._snp_window(snps, max_snps, pos)

        return snps

    def iter_scored_snps(self, assembly, chrom, start, end, base_level=False):
        '''For a region, iteratively yields all SNPs with scores.'''
        if end < start:
            return
        chunk_size = REGDB_SCORE_CHUNK_SIZE
        chunk_start = start
        while chunk_start <= end:
            chunk_end = chunk_start + chunk_size
            if chunk_end > end:
                chunk_end = end
            yield from self._scored_snps(assembly, chrom, chunk_start, chunk_end)
            chunk_start += chunk_size

    def iter_scored_signal(self, assembly, chrom, start, end):
        '''For a region, iteratively yields all bedGraph styled regions
           of contiguous numeric score.'''
        if end < start:
            return
        chunk_size = REGDB_SCORE_CHUNK_SIZE
        chunk_start = start
        while chunk_start <= end:
            chunk_end = chunk_start + chunk_size
            if chunk_end > end:
                chunk_end = end
            yield from self._scored_regions(assembly, chrom, chunk_start, chunk_end)
            chunk_start += chunk_size

    def live_score(self, assembly, chrom, pos):
        '''Returns score knowing single position and nothing more.'''
        (peaks, details) = self.find_peaks_filtered(assembly, chrom, pos, pos)
        if not peaks or not details:
            return None
        (datasets, _files) = self.details_breakdown(details)
        return self.regulome_score(datasets)

    @staticmethod
    def numeric_score(alpha_score):
        '''converst str score to numeric representation (for bedGraph)'''
        try:
            return REGDB_NUM_SCORES[REGDB_STR_SCORES.index(alpha_score)]
        except Exception:
            return 0

    @staticmethod
    def str_score(int_score):
        '''converst numeric representation of score to standard string score'''
        try:
            return REGDB_STR_SCORES[REGDB_NUM_SCORES.index(int_score)]
        except Exception:
            return ''
