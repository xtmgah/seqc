#!/usr/local/bin/python3

import argparse
import multiprocessing
import os
import sys
import seqc
from subprocess import Popen


def parse_args(args):
    p = argparse.ArgumentParser(description='Process Single-Cell RNA Sequencing Data')
    p.add_argument('platform',
                   choices=['in_drop', 'drop_seq', 'mars1_seq',
                            'mars2_seq', 'in_drop_v2'],
                   help='which platform are you merging annotations from?')
    p.add_argument('-g', '--genomic-fastq', nargs='*', metavar='G', default=[],
                   help='fastq file(s) containing genomic information')
    p.add_argument('-b', '--barcode-fastq', nargs='*', metavar='B', default=[],
                   help='fastq file(s) containing barcode information')
    p.add_argument('-m', '--merged-fastq', nargs='?', metavar='M', default='',
                   help='fastq file containing genomic information annotated with '
                        'barcode data')
    p.add_argument('-s', '--samfile', nargs='?', metavar='S', default='',
                   help='sam file containing aligned, merged fastq records.')
    p.add_argument('--basespace', metavar='BS', help='BaseSpace sample ID. '
                                                     'Identifies a sequencing run to download and process.')

    # todo this should be taken from configure/config
    p.add_argument('--basespace-token', metavar='BT', help='BaseSpace access '
                                                           'token')

    p.add_argument('-o', '--output-stem', metavar='O', help='file stem for output files '
                                                            'e.g. ./seqc_output/tumor_run5')
    p.add_argument('-i', '--index', metavar='I', help='Folder or s3 link to folder '
                                                      'containing index files for alignment and resolution of ambiguous '
                                                      'reads.')

    p.add_argument('-v', '--version', action='version',
                   version='{} {}'.format(p.prog, seqc.__version__))

    f = p.add_argument_group('filter arguments')
    f.add_argument('--max-insert-size', metavar='F', help='maximum paired-end insert '
                                                          'size that is considered a valid record',
                   default=1000)
    f.add_argument('--min-poly-t', metavar='T', help='minimum size of poly-T tail that '
                                                     'is required for a barcode to be considered a valid record',
                   default=3)
    f.add_argument('--max-dust-score', metavar='D', help='maximum complexity score for a '
                                                         'read to be considered valid')

    r = p.add_argument_group('run options')
    r.set_defaults(remote=True)
    r.add_argument('--local', dest="remote", action="store_false",
                   help='run SEQC locally instead of initiating on AWS EC2 servers')
    r.add_argument('--email-status', metavar='E', default=None,
                   help='email address to receive run summary when running remotely')
    r.add_argument('--cluster-name', default=None, metavar='C',
                   help='optional name for EC2 instance')
    r.add_argument('--no-terminate', default=False, action='store_true',
                   help='do not terminate the EC2 instance after program completes')
    r.add_argument('--aws-upload-key', default=None, metavar='A',
                   help='location to upload results')

    return p.parse_args(args)


def run_remote(name: str, outdir: str) -> None:
    """
    :param name: cluster name if provided by user, otherwise None
    :param outdir: where seqc will be installed on the cluster
    """
    seqc.log.notify('Beginning remote SEQC run...')

    # recreate remote command, but instruct it to run locally on the server.
    cmd = outdir + '/seqc/src/seqc/process_experiment.py ' + ' '.join(
        sys.argv[1:]) + ' --local'

    # set up remote cluster
    cluster = seqc.remote.ClusterServer()
    cluster.cluster_setup(name)
    cluster.serv.connect()

    # temp_path = seqc.__path__[0]
    # filepath = temp_path + '/instance.txt'
    # todo : not writing this into the local place yet --> just experimenting remote
    # todo | writing the instance file into '~/seqc/instance.txt'
    # todo | can check for dissociated security groups at the start of each SEQC run!
    # inst_path = os.path.expanduser('~/') + 'seqc'
    # mkfile = Popen(['mkdir', inst_path])
    # mkfile.communicate()
    # with open(inst_path + '/instance.txt', 'w') as f:
    #     f.write('%s\n' % str(cluster.inst_id.instance_id))
    #     f.write('%s\n' % str(cluster.inst_id.security_groups[0]['GroupId']))

    seqc.log.notify('Beginning remote run.')
    # writing name of instance in ~/seqc/instance.txt for clean up
    # todo | check if you need sudo here or not
    inst_path = os.path.expanduser('~/') + 'seqc'
    cluster.serv.exec_command('mkdir %s' % inst_path)
    cluster.serv.exec_command('cd {inst_path}; echo {instance_id} > instance.txt'
                              ''.format(inst_path=inst_path,
                                        instance_id=str(cluster.inst_id.instance_id)))
    cluster.serv.exec_command('cd {out}; nohup {cmd} > /dev/null 2>&1 &'
                              ''.format(out=outdir, cmd=cmd))
    seqc.log.notify('Terminating local client. Email will be sent when remote run '
                    'completes.')


def main(args: list = None):
    seqc.log.setup_logger()

    try:
        args = parse_args(args)
        seqc.log.args(args)

        # split output_stem into path and prefix
        output_dir, output_prefix = os.path.split(args.output_stem)

        if args.remote:
            run_remote(args.cluster_name, output_dir)
            sys.exit()

        # do a bit of argument checking
        if args.output_stem.endswith('/'):
            print('-o/--output-stem should not be a directory')
            sys.exit(2)

        # download data if necessary
        if args.basespace:
            seqc.log.info('BaseSpace link provided for fastq argument. Downloading '
                          'input data.')
            if not args.basespace_token:
                raise ValueError(
                    'If the --basespace argument is used, the --basespace-token argument '
                    'must also be provided in order to gain access to the basespace '
                    'repository')
            # accounting for how BaseSpace downloads files
            bspace_dir = output_dir + '/Data/Intensities/BaseCalls/'
            bf = Popen(['sudo', 'mkdir', '-p', bspace_dir])
            bf.communicate()
            bf2 = Popen(['sudo', 'chown', '-c', 'ubuntu', bspace_dir])
            bf2.communicate()
            args.barcode_fastq, args.genomic_fastq = seqc.io.BaseSpace.download(
                args.platform, args.basespace, output_dir, args.basespace_token)

        # check if the index must be downloaded
        if not args.index.startswith('s3://'):
            if not os.path.isdir(args.index):
                raise ValueError('provided index: "%s" is neither an s3 link or a valid '
                                 'filepath' % args.index)
        else:
            try:
                seqc.log.info('AWS s3 link provided for index. Downloading index.')
                bucket, prefix = seqc.io.S3.split_link(args.index)
                args.index = output_dir + '/index/'  # set index  based on s3 download
                cut_dirs = prefix.count('/')
                seqc.io.S3.download_files(bucket, prefix, args.index, cut_dirs)
            except FileNotFoundError:
                raise FileNotFoundError('No index file or folder was identified at the '
                                        'specified s3 index location: %s' % args.index)
            except FileExistsError:
                pass  # file is already present.

        n_processes = multiprocessing.cpu_count() - 1  # get number of processors

        # determine where the script should start:
        merge = True
        align = True
        if args.samfile:
            merge = False
            align = False
        if args.merged_fastq:
            merge = False

        if merge:
            seqc.log.info('Merging genomic reads and barcode annotations.')
            merge_function = getattr(seqc.sequence.merge_functions, args.platform)
            args.merged_fastq = seqc.sequence.fastq.merge_paired(
                merge_function=merge_function,
                fout=args.output_stem + '_merged.fastq',
                genomic=args.genomic_fastq,
                barcode=args.barcode_fastq)

        if align:
            seqc.log.info('Aligning merged fastq records.')
            *base_directory, stem = args.output_stem.split('/')
            alignment_directory = '/'.join(base_directory) + '/alignments/'
            os.makedirs(alignment_directory, exist_ok=True)
            args.samfile = seqc.alignment.star.align(
                args.merged_fastq, args.index, n_processes, alignment_directory)

        seqc.log.info('Filtering aligned records and constructing record database')
        ra = seqc.arrays.ReadArray.from_samfile(
            args.samfile, args.index + 'annotations.gtf')
        ra.save(args.output_stem + '.h5')

        seqc.log.info('Correcting errors and generating expression matrices.')
        # todo add these functions

        seqc.log.info('Run complete.')

        if args.email_status:
            seqc.remote.upload_results(
                args.output_stem, args.email_status, args.aws_upload_key)

    except:
        seqc.log.exception()
        if args.email_status and not args.remote:
            email_body = 'Process interrupted -- see attached error message'
            seqc.remote.email_user(attachment='seqc.log', email_body=email_body,
                                   email_address=args.email_status)
        raise

    finally:
        if not args.remote:
            print('i am being run locally and i am in the terminating zone')
            if not args.no_terminate:
                fpath = os.path.expanduser('~/') + 'seqc/instance.txt'
                if os.path.isfile(fpath):
                    with open(fpath, 'r') as f:
                        inst_id = f.readline().strip('\n')
                    seqc.remote.terminate_cluster(inst_id)
                else:
                    seqc.log.info('file containing instance id is unavailable!')
            else:
                seqc.log.info('not terminating cluster -- user responsible for cleanup')


if __name__ == '__main__':
    main(sys.argv[1:])
