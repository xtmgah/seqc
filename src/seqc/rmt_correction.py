from scipy.special import gammainc
from seqc.sequence.encodings import DNA3Bit
import numpy as np
from seqc import log
from seqc.read_array import ReadArray
import time
import pandas as pd
import multiprocessing as multi
from itertools import repeat
import ctypes
from contextlib import closing
from functools import partial

# todo document me
def generate_close_seq(seq):
    """ Return a list of all sequences that are up to 2 hamm distance from seq
    :param seq:
    """
    res = []
    l = DNA3Bit.seq_len(seq)

    # generate all sequences that are dist 1
    for i in range(l):
        mask = 0b111 << (i * 3)
        cur_chr = (seq & mask) >> (i * 3)
        res += [seq & (~mask) | (new_chr << (i * 3))
                for new_chr in DNA3Bit.bin2strdict.keys() if new_chr != cur_chr]
    # generate all sequences that are dist 2
    for i in range(l):
        mask_i = 0b111 << (i * 3)
        chr_i = (seq & mask_i) >> (i * 3)
        for j in range(i + 1, l):
            mask_j = 0b111 << (j * 3)
            chr_j = (seq & mask_j) >> (j * 3)
            mask = mask_i | mask_j
            res += [seq & (~mask) | (new_chr_i << (i * 3)) | (new_chr_j << (j * 3)) for
                    new_chr_i in DNA3Bit.bin2strdict.keys() if new_chr_i != chr_i for
                    new_chr_j in DNA3Bit.bin2strdict.keys() if new_chr_j != chr_j]

    return res


# todo document me
def probability_for_convert_d_to_r(d_seq, r_seq, err_rate):
    """
    Return the probability of d_seq turning into r_seq based on the err_rate table
    (all binary)

    :param err_rate:
    :param r_seq:
    :param d_seq:
    """

    if DNA3Bit.seq_len(d_seq) != DNA3Bit.seq_len(r_seq):
        return 1

    p = 1.0
    while d_seq > 0:
        if d_seq & 0b111 != r_seq & 0b111:
            if isinstance(err_rate,float):
                p *= err_rate
            else:
                p *= err_rate[(d_seq & 0b111, r_seq & 0b111)]
        d_seq >>= 3
        r_seq >>= 3
    return p


def in_drop(read_array, error_rate, alpha=0.05):
    """ Tag any RMT errors

    :param read_array: Read array
    :param error_rate: Sequencing error rate determined during barcode correction
    :param alpha: Tolerance for errors
    """

    global ra
    global indices_grouped_by_cells

    ra = read_array
    indices_grouped_by_cells = ra.group_indices_by_cell()
    _correct_errors(error_rate, alpha)


# a method called by each process to correct RMT for each cell
def _correct_errors_by_cell_group(err_rate, p_value, cell_index):

    cell_group = indices_grouped_by_cells[cell_index]
    # Breaks for each gene
    gene_inds = cell_group[np.argsort(ra.genes[cell_group])]
    breaks = np.where(np.diff(ra.genes[gene_inds]))[0] + 1
    splits = np.split(gene_inds, breaks)
    rmt_groups = {}
    res = []

    for inds in splits:
        # RMT groups
        for ind in inds:
            rmt = ra.data['rmt'][ind]
            try:
                rmt_groups[rmt].append(ind)
            except KeyError:
                rmt_groups[rmt] = [ind]

        if len(rmt_groups) == 1:
            continue

        # This logic retains RMTs with N if no donor is found and contributes to the
        # molecule count
        for rmt in rmt_groups.keys():

            # Enumerate all possible RMTs with hamming distances 1 and/or 2
            # to build a probablitiy that this particular RMT was not an error
            # Simulatenously, check if Jaitin error correction can be applied
            jaitin_corrected = False
            expected_errors = 0
            for donor_rmt in generate_close_seq(rmt):

                # Check if donor is detected
                try:
                    donor_count = len(rmt_groups[donor_rmt])
                except KeyError:
                    continue

                # Build likelihood
                # Probability of converting donor to target
                p_dtr = probability_for_convert_d_to_r(donor_rmt, rmt, err_rate)
                # Number of occurrences
                expected_errors += donor_count * p_dtr

                # Check if jaitin correction is feasible
                if not jaitin_corrected: 
                    ref_positions = ra.positions[rmt_groups[rmt]]
                    donor_positions = ra.positions[rmt_groups[donor_rmt]]

                    # Is reference a subset of the donor ? 
                    if (set(ref_positions)).issubset(donor_positions):
                        jaitin_corrected = True
                        jaitin_donor = donor_rmt

            # Probability that the RMT is an error
            p_val_err = gammainc(len(rmt_groups[rmt]), expected_errors)

            # Remove Jaitin corrected reads if probability of RMT == error is high
            if p_val_err > p_value and jaitin_corrected:
                # Save the RMT donor
                # save the index of the read and index of donor rmt read
                for i in rmt_groups[rmt]:
                    res.append(i)
                    res.append(rmt_groups[jaitin_donor][0])

        rmt_groups.clear()

    return res


def _correct_errors(err_rate, p_value=0.05):
    #Calculate and correct errors in RMTs
    with multi.Pool(processes=multi.cpu_count()) as p:
        p = multi.Pool(processes=multi.cpu_count())
        results = p.starmap(_correct_errors_by_cell_group, 
                          zip(repeat(err_rate), repeat(p_value), range(len(indices_grouped_by_cells))))
        p.close()
        p.join()

        # iterate through the list of returned read indices and donor rmts 
        for i in range(len(results)):
            res = results[i]
            if len(res) > 0:
                for i in range(0, len(res), 2):
                    ra.data['rmt'][res[i]] = ra.data['rmt'][res[i+1]]
                    ra.data['status'][res[i]] |= ra.filter_codes['rmt_error']