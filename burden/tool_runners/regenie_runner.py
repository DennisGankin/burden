import re
import csv
import pandas as pd

from pathlib import Path
from typing import Tuple, List

from burden.tool_runners.tool_runner import ToolRunner
from general_utilities.job_management.thread_utility import ThreadUtility
from general_utilities.association_resources import get_chromosomes, define_covariate_string, \
    define_field_names_from_tarball_prefix, bgzip_and_tabix
from general_utilities.import_utils.import_lib import process_bgen_file
from general_utilities.plot_lib.manhattan_plotter import ManhattanPlotter


# TODO: Implement multi-phenotype testing for REGENIE
# WHAAAAAAAT?! not implemented yet???!!!
class REGENIERunner(ToolRunner):

    def run_tool(self) -> None:
        # quick file check to make sure all files available
        if not self._check_required_files():
            self._logger.error("Required files are missing.")
            return 

        # 1. Run step 1 of regenie
        self._logger.info("Running REGENIE step 1")
        regenie_step1_log = self._run_regenie_step_one()
        # log the step1 log content
        with regenie_step1_log.open('r') as regenie_step1_reader:
            for line in regenie_step1_reader:
                self._logger.info(line.strip())
        # Add the step1 files to output, so we can use later if need-be:
        self._outputs.extend([Path('fit_out_pred.list'),
                              Path('fit_out_1.loco'),
                              regenie_step1_log])
        self._logger.info("REGENIE step 1 completed successfully.")

        return

        # tar the whole /test directory and upload to dx
        #self._logger.info("Archiving and uploading /test directory to dx")
        #cmd = f"tar -czf /test.tar.gz /test && dx upload /test.tar.gz --brief"
        #self._association_pack.cmd_executor.run_cmd_on_docker(cmd)

        # 2. Prep bgen files for a run:
        self._logger.info("Downloading and filtering raw bgen files")
        thread_utility = ThreadUtility(self._association_pack.threads,
                                       error_message='A REGENIE bgen thread failed',
                                       incrementor=10,
                                       thread_factor=4)

        for chromosome in get_chromosomes():
            # This makes use of a utility class from AssociationResources since bgen filtering/processing is
            # IDENTICAL to that done for BOLT.
            thread_utility.launch_job(class_type=process_bgen_file,
                                      chrom_bgen_index=self._association_pack.bgen_dict[chromosome],
                                      chromosome=chromosome)
        thread_utility.collect_futures()

        # 3. Prep mask files
        self._logger.info("Prepping mask files")
        #thread_utility = ThreadUtility(self._association_pack.threads,
        #                               error_message='A REGENIE mask thread failed',
        #                               incrementor=10,
        #                               thread_factor=1)
        #for chromosome in get_chromosomes():
        #    for tarball_prefix in self._association_pack.tarball_prefixes:
        #        if Path(f'{tarball_prefix}.{chromosome}.variants_table.STAAR.tsv').exists():
        #            thread_utility.launch_job(class_type=self._make_regenie_files,
        #                                      tarball_prefix=tarball_prefix,
        #                                      chromosome=chromosome)
        #thread_utility.collect_futures()
        self._logger.info("Skipping file generation")

        # 4. Run step 2 of regenie
        self._logger.info("Running REGENIE step 2")
        thread_utility = ThreadUtility(self._association_pack.threads,
                                       error_message='A REGENIE step 2 thread failed',
                                       incrementor=10,
                                       thread_factor=1)
        for chromosome in get_chromosomes():
            for tarball_prefix in self._association_pack.tarball_prefixes:
                if Path(f'{tarball_prefix}.{chromosome}.REGENIE.annotationFile.txt').exists() and Path(
                        f'{tarball_prefix}.{chromosome}.REGENIE.setListFile.txt').exists() and Path(
                        f'{tarball_prefix}.{chromosome}.REGENIE.maskfile.txt').exists():
                    thread_utility.launch_job(self._run_regenie_step_two,
                                              tarball_prefix=tarball_prefix,
                                              chromosome=chromosome)
                else:
                    self._logger.warning(f"Skipping REGENIE step 2 for {tarball_prefix} on chromosome {chromosome} "
                                         f"because one or more required files are missing.")

        # Gather preliminary results from step 2:
        self._logger.info("Gathering REGENIE mask-based results...")
        completed_gene_tables = []
        regenie_step2_genes_log = Path(f'{self._output_prefix}.REGENIE_step2_genes.log')
        with regenie_step2_genes_log.open('w') as regenie_step2_genes_writer:
            for result in thread_utility:
                tarball_prefix, finished_chromosome, current_log = result
                completed_gene_tables.append(self._process_regenie_output(tarball_prefix,
                                                                          finished_chromosome))

                # Write a header for each logfile
                regenie_step2_genes_writer.write(f'{tarball_prefix + "-" + finished_chromosome:{"-"}^{50}}')

                with current_log.open('r') as current_log_reader:
                    for line in current_log_reader:
                        regenie_step2_genes_writer.write(line)

        self._outputs.append(regenie_step2_genes_log)

        # 5. Run per-marker tests, if requested
        completed_marker_chromosomes = []
        if self._association_pack.run_marker_tests:

            self._logger.info("Running per-marker tests...")
            thread_utility = ThreadUtility(self._association_pack.threads,
                                           error_message='A SAIGE marker thread failed',
                                           incrementor=1,
                                           thread_factor=4)
            for chromosome in get_chromosomes():
                thread_utility.launch_job(class_type=self._regenie_marker_run,
                                          chromosome=chromosome)
                completed_marker_chromosomes.append(chromosome)

            regenie_step2_markers_log = Path(f'{self._output_prefix}.REGENIE_step2_markers.log')
            with regenie_step2_markers_log.open('w') as regenie_step2_markers_writer:
                for result in thread_utility:
                    finished_chromosome, current_log = result

                    regenie_step2_markers_writer.write(f'{finished_chromosome:-^50}')
                    with current_log.open('r') as current_log_reader:
                        for line in current_log_reader:
                            regenie_step2_markers_writer.write(line)

            self._outputs.append(regenie_step2_markers_log)

        # 6. Process outputs
        self._logger.info("Processing REGENIE outputs...")
        self._outputs.extend(self._annotate_regenie_output(completed_gene_tables, completed_marker_chromosomes))

    def _check_required_files(self):
        for chromosome in get_chromosomes():
            for tarball_prefix in self._association_pack.tarball_prefixes:
                if not (Path(f'{tarball_prefix}.{chromosome}.REGENIE.annotationFile.txt').exists() and Path(
                    f'{tarball_prefix}.{chromosome}.REGENIE.setListFile.txt').exists() and Path(
                    f'{tarball_prefix}.{chromosome}.REGENIE.maskfile.txt').exists()):
                        self._logger.warning(f"Missing files for {tarball_prefix}.{chromosome}")
                        return False
        self._logger.info(f"All required files found for {tarball_prefix}.{chromosome}")
        return True
                        
    # We need three files per chromosome-mask combination:
    # 1. Annotation file, which lists variants with gene and mask name
    # 2. Set list file, which lists all variants per-gene
    # 3. A mask name file, which lists all masks to run
    # This function handles creation of those files
    @staticmethod
    def _make_regenie_files(tarball_prefix: str, chromosome: str) -> None:

        # This is used to print the set list file (2) below
        gene_dict = {}
        prefix = f'{tarball_prefix}.{chromosome}'

        # 1. Annotation File
        with Path(f'{prefix}.REGENIE.annotationFile.tsv').open('w') as annotation_file,\
                Path(f'{prefix}.variants_table.STAAR.tsv').open('r') as table_file:

            table_reader = csv.DictReader(table_file, delimiter='\t')
            annotation_writer = csv.DictWriter(annotation_file,
                                               delimiter='\t',
                                               fieldnames=['varID', 'ENST', 'annotation'],
                                               extrasaction='ignore',
                                               lineterminator='\n')  # REGENIE is very fussy about line terminators.
            last_var = None  # Need to check for small number of duplicate variants...
            for variant in table_reader:
                if last_var != variant['varID']:
                    variant['annotation'] = tarball_prefix
                    annotation_writer.writerow(variant)
                    last_var = variant['varID']
                    # And build gene_dict while we iterate...
                    if variant['ENST'] in gene_dict:
                        gene_dict[variant['ENST']]['varIDs'].append(variant['varID'])
                    else:
                        gene_dict[variant['ENST']] = {'chrom': variant['chrom'],
                                                      'pos': variant['pos'],
                                                      'varIDs': [variant['varID']],
                                                      'ENST': variant['ENST']}

        # 2. Set list file
        with Path(f'{prefix}.REGENIE.setListFile.tsv').open('w') as set_list_file:
            set_list_writer = csv.DictWriter(set_list_file,
                                             delimiter="\t",
                                             fieldnames=['ENST', 'chrom', 'pos', 'varIDs'],
                                             lineterminator='\n')
            for gene in sorted(list(gene_dict.values()), key=lambda item: item['pos']):
                gene['varIDs'] = ','.join(gene['varIDs'])
                set_list_writer.writerow(gene)

        # 3. This makes the mask name file. Just needs to be the name of the mask (tarball prefix) used in file #1
        with Path(f'{prefix}.REGENIE.maskfile.tsv').open('w') as mask_file:
            mask_file.write(f'{tarball_prefix}\t{tarball_prefix}\n')

    def _run_regenie_step_one(self) -> Path:

        # And generate a SNP list for the --extract parameter of REGENIE, while considering SNPs from
        # the regenie_smaller_snps input parameter (if provided). plink2 order of operations:
        # 1. Select variants from --extract (if present)
        # 2. THEN filter based on max/min AC (mac/max-mac)
        max_mac = (self._sample_count * 2) - 100
        cmd = f'plink2 --bfile /test/genetics/UKBB_470K_Autosomes_QCd_WBA ' \
              f'--min-ac 100 ' \
              f'--max-ac {str(max_mac)}' \
              f' --write-snplist ' \
              f'--out /test/REGENIE_extract'

        if self._association_pack.regenie_snps_file is not None:
            cmd += f' --extract /test/genetics/{self._association_pack.regenie_snps_file.name}'
        self._association_pack.cmd_executor.run_cmd_on_docker(cmd, stdout_file=Path('plink_out.txt'))

        with open('plink_out.txt', 'r') as plink_out:
            for line in plink_out:
                found_snp_count = re.search('(\\d+) variants remaining after main filters', line)
                if found_snp_count is not None:
                    self._logger.info(f'Number of SNPs for REGENIE Step 1: {found_snp_count.group(1)}')
            plink_out.close()

        cmd = 'regenie ' \
              '--step 1 ' \
              '--bed /test/genetics/UKBB_470K_Autosomes_QCd_WBA ' \
              '--extract /test/REGENIE_extract.snplist ' \
              '--covarFile /test/phenotypes_covariates.formatted.txt ' \
              '--phenoFile /test/phenotypes_covariates.formatted.txt ' \
              '--maxCatLevels 110 ' \
              '--bsize 1000 ' \
              '--out /test/fit_out ' \
              f'--threads {str(self._association_pack.threads)} ' \
              f'--phenoCol {self._association_pack.pheno_names[0]} '

        cmd += define_covariate_string(self._association_pack.found_quantitative_covariates,
                                       self._association_pack.found_categorical_covariates,
                                       self._association_pack.is_binary,
                                       add_array=False,
                                       ignore_base=self._association_pack.ignore_base_covariates)

        regenie_log = Path(f'{self._output_prefix}.REGENIE_step1.log')
        self._association_pack.cmd_executor.run_cmd_on_docker(cmd, stdout_file=regenie_log)
        return regenie_log

    def _run_regenie_step_two(self, tarball_prefix: str, chromosome: str) -> Tuple[str, str, Path]:

        # Note – there is some issue with skato (in --vc-tests flag), so I have changed to skato-acat which works...?
        cmd = f'regenie ' \
              f'--step 2 ' \
              f'--bgen /test/filtered_bgen/{chromosome}.filtered.bgen ' \
              f'--sample /test/filtered_bgen/{chromosome}.filtered.sample ' \
              f'--keep /test/SAMPLES_Include.txt ' \
              f'--covarFile /test/phenotypes_covariates.formatted.txt ' \
              f'--phenoFile /test/phenotypes_covariates.formatted.txt ' \
              f'--phenoCol {self._association_pack.pheno_names[0]} ' \
              f'--pred /test/fit_out_pred.list ' \
              f'--anno-file /test/{tarball_prefix}.{chromosome}.REGENIE.annotationFile.txt ' \
              f'--mask-def /test/{tarball_prefix}.{chromosome}.REGENIE.maskfile.txt ' \
              f'--set-list /test/{tarball_prefix}.{chromosome}.REGENIE.setListFile.txt ' \
              f'--aaf-bins 1 ' \
              f'--vc-tests skato-acat,acato-full ' \
              f'--bsize 400 ' \
              f'--threads 1 ' \
              f'--minMAC 1 ' \
              f'--maxCatLevels 110 ' \
              f'--out /test/{tarball_prefix}.{chromosome} '

        cmd += define_covariate_string(self._association_pack.found_quantitative_covariates,
                                       self._association_pack.found_categorical_covariates,
                                       self._association_pack.is_binary,
                                       add_array=False,
                                       ignore_base=self._association_pack.ignore_base_covariates)

        regenie_log = Path(f'{tarball_prefix}.{chromosome}.REGENIE_genes.log')
        self._association_pack.cmd_executor.run_cmd_on_docker(cmd, stdout_file=regenie_log)

        return tarball_prefix, chromosome, regenie_log

    def _regenie_marker_run(self, chromosome: str) -> Tuple[str, Path]:

        cmd = f'regenie ' \
              f'--step 2 ' \
              f'--bgen /test/filtered_bgen/{chromosome}.filtered.bgen ' \
              f'--sample /test/filtered_bgen/{chromosome}.filtered.sample ' \
              f'--keep /test/SAMPLES_Include.txt ' \
              f'--covarFile /test/phenotypes_covariates.formatted.txt ' \
              f'--phenoFile /test/phenotypes_covariates.formatted.txt ' \
              f'--phenoCol {self._association_pack.pheno_names[0]} ' \
              f'--pred /test/fit_out_pred.list ' \
              f'--maxCatLevels 110 ' \
              f'--bsize 200 ' \
              f'--threads 4 ' \
              f'--out /test/{chromosome}.markers.REGENIE '

        cmd += define_covariate_string(self._association_pack.found_quantitative_covariates,
                                       self._association_pack.found_categorical_covariates,
                                       self._association_pack.is_binary,
                                       add_array=False,
                                       ignore_base=self._association_pack.ignore_base_covariates)

        regenie_log = Path(f'{chromosome}.REGENIE_markers.log')
        self._association_pack.cmd_executor.run_cmd_on_docker(cmd, stdout_file=regenie_log)

        return chromosome, regenie_log

    def _process_regenie_output(self, tarball_prefix: str, chromosome: str) -> pd.DataFrame:

        # Load the raw table
        regenie_table = pd.read_csv(f'{tarball_prefix}.{chromosome}_{self._association_pack.pheno_names[0]}.regenie',
                                    sep=' ',
                                    comment='#')

        # And then should be able to split into 3 columns:
        regenie_table[['ENST', 'MASK', 'SUBSET']] = regenie_table['ID'].str.split('.', expand=True)

        # And only take the 'all' subset. Singleton is meaningless here
        regenie_table = regenie_table[regenie_table['SUBSET'] == 'all']

        # Convert -log10(p) back into a p. value:
        regenie_table['PVALUE'] = 10 ** (-1 * regenie_table['LOG10P'])

        # And finally drop columns we won't care about:
        regenie_table = regenie_table.drop(columns=['ID', 'CHROM', 'GENPOS', 'ALLELE0', 'ALLELE1', 'EXTRA',
                                                    'MASK', 'SUBSET', 'LOG10P'])

        # Get column names for Mask/MAF information if possible from the tarball name
        regenie_table = define_field_names_from_tarball_prefix(tarball_prefix, regenie_table)

        # Here we pull out all the various tests that were performed and make a separate table
        p_val_table = regenie_table.pivot(index='ENST', columns='TEST', values='PVALUE')
        p_val_table = p_val_table.drop(columns=['ADD'])

        # And then merge them back into the original table with just the 'additive' p. value
        regenie_table = regenie_table.set_index('ENST')
        regenie_table = regenie_table[regenie_table['TEST'] == 'ADD']
        regenie_table = regenie_table.merge(p_val_table, left_index=True, right_index=True)

        # And finally add an AC column
        regenie_table['AC'] = regenie_table['A1FREQ'] * (regenie_table['N'] * 2)
        regenie_table['AC'] = regenie_table['AC'].round()

        return regenie_table

    def _annotate_regenie_output(self, completed_gene_tables: List[pd.DataFrame],
                                 completed_marker_chromosomes: List[str]) -> List[Path]:

        # Declare a list for storing outputs
        plot_dir = Path(f'{self._output_prefix}_plots/')  # Path to store plots
        plot_dir.mkdir()
        outputs = [plot_dir]
        regenie_table = pd.concat(completed_gene_tables)

        # Now merge the transcripts table into the gene table to add annotation and then write
        # try this if does not work skip:
        try:
            regenie_table = pd.merge(self._transcripts_table, regenie_table, left_index=True, right_index=True, how="left")
        except:
            self._logger.warning("Failed to merge transcripts table with REGENIE gene table. "
                                 "This may be due to missing ENST IDs in the transcripts table.")
            # print head of both
            self._logger.info("REGENIE gene table head:\n" + regenie_table.head().to_string())
            self._logger.info("Transcripts table head:\n" + self._transcripts_table.head().to_string())

        regenie_gene_out = Path(f'{self._output_prefix}.genes.REGENIE.stats.tsv')
        with regenie_gene_out.open('w') as gene_out:

            # Reset the index and make sure chrom/start/end are first (for indexing)
            regenie_table.reset_index(inplace=True)
            columns = regenie_table.columns.tolist()

            # ENST should ALWAYS be in position 0, but move it to position 4 and slice the array so we don't have two
            # copies:

            columns = columns[1:]
            regenie_table = regenie_table[columns]

            # Sort just in case
            regenie_table = regenie_table.sort_values(by=['chrom', 'start', 'end'])

            # skip if mask not in columns
            if 'MASK' in regenie_table.columns and 'MAF' in regenie_table.columns:
                for mask in regenie_table['MASK'].value_counts().index:

                    for maf in regenie_table['MAF'].value_counts().index:
                        # To note on the below: I use SYMBOL for the id_column parameter below because ENST is the
                        # index and I don't currently have a way to pass the index through to the Plotter methods...
                        manhattan_plotter = ManhattanPlotter(self._association_pack.cmd_executor,
                                                            regenie_table.query(f'MASK == "{mask}" & MAF == "{maf}"'),
                                                            chrom_column='chrom', pos_column='start',
                                                            alt_column=None,
                                                            id_column='SYMBOL', p_column='PVALUE',
                                                            csq_column='MASK',
                                                            maf_column='A1FREQ', gene_symbol_column='SYMBOL',
                                                            clumping_distance=1,
                                                            maf_cutoff=30 / (regenie_table['N'].max() * 2),
                                                            sig_threshold=1E-6)

                        manhattan_plotter.plot()[0].rename(plot_dir / f'{mask}.{maf}.genes.REGENIE.png')

            # Write to disk
            regenie_table.to_csv(path_or_buf=gene_out, index=False, sep="\t", na_rep='NA')

        # And bgzip and tabix...
        outputs.extend(bgzip_and_tabix(regenie_gene_out, comment_char='c', sequence_row=1, begin_row=2, end_row=3))

        if self._association_pack.run_marker_tests:

            variant_index = []
            regenie_table_marker = []
            # Open all chromosome indicies and load them into a list and append them together
            for chromosome in completed_marker_chromosomes:
                variant_index.append(
                    pd.read_csv(f'filtered_bgen/{chromosome}.filtered.vep.tsv.gz',
                                sep="\t",
                                dtype={'SIFT': str, 'POLYPHEN': str}))
                regenie_table_marker.append(
                    pd.read_csv(f'{chromosome}.markers.REGENIE_{self._association_pack.pheno_names[0]}.regenie',
                                sep=' '))

            variant_index = pd.concat(variant_index)
            variant_index = variant_index.set_index('varID')

            regenie_table_marker = pd.concat(regenie_table_marker)

            # For markers, we can use the SNP ID column to get what we need
            regenie_table_marker = regenie_table_marker.rename(
                columns={'ID': 'varID', 'A1FREQ': 'REGENIE_MAF'})
            regenie_table_marker['PVALUE'] = 10 ** (-1 * regenie_table_marker['LOG10P'])
            regenie_table_marker = regenie_table_marker.drop(columns=['CHROM', 'GENPOS', 'ALLELE0', 'ALLELE1', 'INFO',
                                                                      'EXTRA', 'TEST', 'LOG10P'])
            regenie_table_marker = pd.merge(variant_index, regenie_table_marker, on='varID', how="left")
            regenie_marker_out = Path(f'{self._output_prefix}.markers.REGENIE.stats.tsv')
            with regenie_marker_out.open('w') as marker_out:
                # Sort by chrom/pos just to be sure...
                regenie_table_marker = regenie_table_marker.sort_values(by=['CHROM', 'POS'])
                regenie_table_marker.to_csv(path_or_buf=marker_out, index=False, sep="\t", na_rep='NA')

            outputs.extend(bgzip_and_tabix(regenie_marker_out, skip_row=1, sequence_row=2, begin_row=3, end_row=3))

        return outputs
