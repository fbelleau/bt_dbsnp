import json
import glob

from utils.hgvs import get_pos_start_end, trim_delseq_from_hgvs
from biothings.utils.dataload import dict_sweep, unlist, \
                                     value_convert_to_number
from biothings.utils.common import open_compressed_file

# QUESTION:
# rs5823327 => no ref, alt; do we still want to keep it
# rs10577038 => two different hgvs representations
#               NC_000018.9:g.476589_476590delGC
#               NC_000018.9:g.476588_476590delCGCinsA
# rs16442   => novel patch
#               NC_000021.8:g.34821649_34821650delAA
#               NW_003315970.1:g.43915_43916delAA
# geneid is going to be integer (previously string)
# fields gone: allele_origin, gmaf, alleles/allele/freq, class, flags, validated, var_subtype
# current code unable to get assembly start/end position based on vcf for indel/del/is
# TODO
# Index geneid field as string


def parse_one_rec(assembly, record):
    """Restructure JSON
    """
    doc = {"alleles": [], "gene": [],
           assembly: {},
           "vartype": record.get("primary_snapshot_data").get("variant_type"),
           "rsid": "rs" + str(record.get("refsnp_id")),
           "dbsnp_build": int(record.get("last_update_build_id")),
           "dbsnp_merges": restructure_dbsnp_merge(record.get("dbsnp1_merges")),
           "citations": record.get("citations")}
    data = record.get('primary_snapshot_data')
    hgvs_vcf_info = get_hgvs_and_vcf(assembly,
                                     data.get("placements_with_allele"))
    allele_annotations = data.get('allele_annotations')
    allele_annotations = list(allele_annotations)
    doc["alleles"] = restructure_allele_freq_info(allele_annotations)
    doc['gene'] = restructure_gene_info(allele_annotations)
    for _item in hgvs_vcf_info:
        hgvs, vcf = _item
        if vcf:
            doc["chrom"], pos, doc["ref"], doc["alt"] = vcf
            doc["chrom"] = str(doc["chrom"])
            if doc["chrom"] == "23":
                doc["chrom"] = "X"
            elif doc["chrom"] == "24":
                doc["chrom"] = "Y"
            doc[assembly] = {}
            try:
                if doc["vartype"] != "snv":
                    ref = "T" + doc["ref"]
                    alt = "T" + doc["alt"]
                else:
                    ref = doc["ref"]
                    alt = doc["alt"]
                if doc["vartype"] in ["ins", "del", "delins"]:
                    doc[assembly]['start'], doc[assembly]['end'] = get_pos_start_end(doc["chrom"], pos - 1, ref, alt)
                else:
                    doc[assembly]['start'], doc[assembly]['end'] = get_pos_start_end(doc["chrom"], pos, ref, alt)
            except (ValueError, AssertionError):
                doc[assembly] = {}
        if hgvs:
            doc["_id"] = hgvs.replace('chr23', 'chrX').replace('chr24', 'chrY')
            yield dict_sweep(unlist(value_convert_to_number(doc, skipped_keys=['chrom', 'ref', 'alt', 'allele', 'deleted_sequence', 'inserted_sequence'])), vals=[[], {}, None])


def restructure_allele_freq_info(allele_annotations):
    """Restructure information related to allele frequency
    """
    alleles_data = []
    for _annotation in allele_annotations:
        freq_data = _annotation.get('frequency')
        if freq_data:
            freq = {'freq': {}}
            freq_data = list(freq_data)
            for _doc in freq_data:
                if _doc:
                    freq['allele'] = _doc.get('observation').get('inserted_sequence')
                    freq_source = _doc.get('study_name').lower()
                    if freq_source == '1000genomes':
                        freq_source = '1000g'
                    freq['freq'][freq_source] = round(_doc.get('allele_count')/_doc.get('total_count'), 3)
            alleles_data.append(freq)
    return alleles_data


"""
def normalize_delins_hgvs(hgvs):
    # handle delins, where no deleted nucleotides is specified
    if 'delins' in hgvs:
        return hgvs
    # handle delins, where deleted nucleotides is specified
    elif 'del' in hgvs and 'ins' in hgvs:
        prefix, suffix = hgvs.split('del')
        deleted, inserted = suffix.split('ins')
        return prefix + 'delins' + inserted
    # handle deletions
    elif 'del' in hgvs:
        return hgvs.split('del')[0] + 'del'
    # handle snv, ins, dup
    elif '>' in hgvs or 'ins' in hgvs or 'dup' in hgvs or '[' in hgvs or 'inv' in hgvs:
        return hgvs
    else:
        print('hgvs not delins or snv or dup', hgvs)
        return hgvs
"""


def restructure_gene_info(allele_annotations):
    """Restructure information related to gene
    """
    gene_data = []
    assembly_annotation = allele_annotations[0].get('assembly_annotation')
    if assembly_annotation and assembly_annotation[0]:
        for _doc in assembly_annotation[0].get('genes'):
            if _doc:
                if "orientation" in _doc:
                    _doc['strand'] = _doc.pop("orientation")
                if _doc["strand"] == "plus":
                    _doc["strand"] = "+"
                elif _doc["strand"] == "minus":
                    _doc["strand"] = "-"
                _doc['geneid'] = _doc.pop('id')
                _doc['symbol'] = _doc.pop('locus')
                _doc['so'] = _doc.pop('sequence_ontology')
                for _item in _doc['rnas']:
                    if _item:
                        _item['refseq'] = _item.pop('id')
                        _item['so'] = _item.pop('sequence_ontology')
                        if 'product_id' in _item:
                            _item['protein_product'] = {'refseq': None}
                            _item['protein_product']['refseq'] = _item.pop('product_id')
                gene_data.append(_doc)
    return gene_data


def restructure_dbsnp_merge(merged_data):
    if merged_data:
        for _doc in merged_data:
            _doc["rsid"] = "rs" + _doc.pop("merged_rsid")
            _doc["date"] = _doc.pop('merge_date')
            _doc["rv"] = _doc.pop("revision")
    return merged_data


def accession_2_chr(accession):
    if accession.startswith('NC_0000'):
        return str(int(accession[7:9]))
    else:
        return 'MT'


def get_hgvs_and_vcf(assembly, placements):
    # Note that hg38 data of dbsnp release 154 are based on "GRCh38.p12",
    #   while release 155 based on "GRCh38.p13"
    ASSEMBLY_NAME_MAPPING = {"hg19": "GRCh37.p13", "hg38": "GRCh38.p13"}

    if placements:
        for _placement in placements:
            seq = _placement.get('placement_annot').get('seq_id_traits_by_assembly')
            if seq:
                placement_assembly_name = seq[0].get('assembly_name')
                expected_assembly_name = ASSEMBLY_NAME_MAPPING[assembly]

                if placement_assembly_name == ASSEMBLY_NAME_MAPPING[assembly]:
                    for _allele in _placement.get('alleles'):
                        if _allele.get('allele').get('spdi').get('deleted_sequence') != _allele.get('allele').get('spdi').get('inserted_sequence') and _allele.get('hgvs').startswith('NC'):
                            hgvs = 'chr' + accession_2_chr(_allele.get('hgvs')) + ":" + _allele.get('hgvs').split(':')[-1]
                            # ref = _allele.get("allele").get('spdi').get('deleted_sequence')
                            # alt = _allele.get("allele").get('spdi').get('inserted_sequence')
                            vcf = (accession_2_chr(_allele.get('allele').get('spdi').get('seq_id')),
                                   _allele.get("allele").get('spdi').get('position') + 1,
                                   _allele.get("allele").get('spdi').get('deleted_sequence'),
                                   _allele.get("allele").get('spdi').get('inserted_sequence'))
                            yield hgvs, vcf
                else:
                    # Take "GRCh38.p13" as an example.
                    #   "GRCh38" is the build number; "p13" is the release number
                    placement_grch_build = placement_assembly_name.split(r".", 1)[0]
                    expected_grch_build = expected_assembly_name.split(r".", 1)[0]

                    if placement_grch_build == expected_grch_build:
                        raise ValueError("GRCh release numbers do not match. Expect {}. Got {}.".format(
                            expected_assembly_name, placement_assembly_name))

    yield None, None


def load_data_file(input_file, version):
    f = open_compressed_file(input_file)
    for line in f:
        record = parse_one_rec(version, json.loads(line.decode()))
        for _doc in record:
            new_doc = dict()
            new_doc['_id'] = trim_delseq_from_hgvs(_doc.pop('_id'))
            new_doc['dbsnp'] = _doc
            yield new_doc


# load path and find files, pass to data_generator
def load_data(path_glob, version='hg19'):
    for input_file in sorted(glob.glob(path_glob)):
        for d in load_data_file(input_file, version):
            yield d
