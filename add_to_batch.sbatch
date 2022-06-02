#!/bin/bash

##################
### sbatch configuration parameters must start with #SBATCH and must precede any other commands.
### To ignore, just add another # - like ##SBATCH
##################

#SBATCH --partition main			
#SBATCH --time 7-00:00:00			### limit the time of job running, partition limit can override this. Format: D-H:MM:SS
#SBATCH --job-name score_SDE			### name of the job
#SBATCH --output /home/tohamy/BNP/SDE/score_sde_pytorch/log.log			### output log for running job - %J for job number
#SBATCH --mail-user=tohamy@post.bgu.ac.il	### user email for sending job status
#SBATCH --mail-type=ALL			### conditions when to send the email. ALL,BEGIN,END,FAIL, REQUEU, NONE

#SBATCH --gpus=rtx_2080:1				### number of GPUs, ask for more than 1 only if you can parallelize your code for multi GPU
##SBATCH --qos=orenfr
#SBATCH --mem=32G				### ammount of RAM memory
#SBATCH --cpus-per-task=6			### number of CPU cores
#SBATCH --tmp=250G

# ##For issues CUDA\gcc\C++V
# scl enable devtoolset-9 bash
# export LD_LIBRARY_PATH=/home/ronsha/.conda/envs/dtan_env/lib:$LD_LIBRARY_PATH
# export PATH=/opt/rh/devtoolset-9/root/usr/bin/:$PATH

##For issues CUDA\gcc\C++V
scl enable devtoolset-9 bash
export LD_LIBRARY_PATH=/home/tohamy/.conda/envs/iritenv/lib:$LD_LIBRARY_PATH
export PATH=/opt/rh/devtoolset-9/root/usr/bin/:$PATH

which gcc
gcc --version

### Print some data to output file ###
echo `date`
echo -e "\nSLURM_JOBID:\t\t" $SLURM_JOBID
echo -e "SLURM_JOB_NODELIST:\t" $SLURM_JOB_NODELIST "\n\n"

##export SLURM_ARRAY_TASK_ID=SLURM_ARRAY_TASK_ID:$SLURM_ARRAY_TASK_ID ##not sure if needed

### Start your code below ####
module load anaconda				### load anaconda module (must present when working with conda environments)
module load cuda/11.4
source activate iritenv				### activating environment, environment must be configured before running the job
###~/.conda/envs/iritenv/bin/python main.py --config configs/ve/cifar10_ncsnpp_deep_continuous.py --workdir output/ --eval_folder output/eval_folder/ --mode train
~/.conda/envs/iritenv/bin/python main.py --config configs/ve/cifar10_ddpm.py --workdir output/ --eval_folder output/eval_folder/ --mode train